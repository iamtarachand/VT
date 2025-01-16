import base64
import html
import logging
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from http.cookiejar import MozillaCookieJar

import click
from appdirs import AppDirs
from pymediainfo import MediaInfo
from vinetrimmer.vendor.pymp4.parser import Box

from vinetrimmer import services
from vinetrimmer.config import config, credentials, directories, filenames
from vinetrimmer.objects import AudioTrack, Credential, TextTrack, Title, Titles, VideoTrack
from vinetrimmer.objects.vaults import Vault, Vaults
from vinetrimmer.utils import Cdm, Logger
from vinetrimmer.utils.click import LANGUAGE_RANGE, QUALITY, SEASON_RANGE, AliasedGroup, ContextData
from vinetrimmer.utils.collections import as_list, merge_dict
from vinetrimmer.utils.io import load_toml
from vinetrimmer.utils.subprocess import ffprobe
from vinetrimmer.utils.widevine.device import LocalDevice, RemoteDevice


def get_logger(level=logging.INFO, log_path=None):
    """
    Setup logging for the Downloader.
    If log_path is not set or false-y, logs won't be stored.
    """
    log = Logger.getLogger("dl", level=level)
    if log_path:
        if not os.path.isabs(log_path):
            log_path = os.path.join(directories.logs, log_path)
        log_path = log_path.format_map(defaultdict(
            str,
            name="root",
            time=datetime.now().strftime("%Y%m%d-%H%M%S")
        ))
        if os.path.exists(os.path.dirname(log_path)):
            log_files = [
                x for x in os.listdir(os.path.dirname(log_path))
                if os.path.splitext(x)[1] == os.path.splitext(log_path)[1]
            ]
            for log_file in log_files[::-1][19:]:
                # keep the 20 newest files and delete the rest
                os.unlink(os.path.join(os.path.dirname(log_path), log_file))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log.add_file_handler(log_path)
    return log


def get_cdm(service, profile=None):
    """
    Get CDM Device (either remote or local) for a specified service.
    Raises a ValueError if there's a problem getting a CDM.
    """
    cdm_name = config.cdm.get(service) or config.cdm.get("default")
    if not cdm_name:
        raise ValueError("A CDM to use wasn't listed in the vinetrimmer.toml config")
    if isinstance(cdm_name, dict):
        if not profile:
            raise ValueError("CDM config is mapped for profiles, but no profile was chosen")
        cdm_name = cdm_name.get(profile) or config.cdm.get("default")
        if not cdm_name:
            raise ValueError(f"A CDM to use was not mapped for the profile {profile}")
    cdm_api = next(iter(x for x in config.cdm_api if x["name"] == cdm_name), None)
    if cdm_api:
        return RemoteDevice(**cdm_api)
    try:
        return LocalDevice.load(os.path.join(directories.devices, f"{cdm_name}.wvd"))
    except FileNotFoundError:
        dirs = [
            os.path.join(directories.devices, cdm_name),
            os.path.join(AppDirs("pywidevine", False).user_data_dir, "devices", cdm_name),
            os.path.join(AppDirs("pywidevine", False).site_data_dir, "devices", cdm_name),
        ]

        for d in dirs:
            try:
                return LocalDevice.from_dir(d)
            except FileNotFoundError:
                pass

        raise ValueError(f"Device {cdm_name!r} not found")


def get_service_config(service):
    """Get both Service Config and Service Secrets as one merged dictionary."""
    service_config = load_toml(filenames.service_config.format(service=service))
    user_config = load_toml(filenames.user_service_config.format(service=service))
    if user_config:
        merge_dict(service_config, user_config)
    return service_config


def get_profile(service):
    """
    Get the default profile for a service from the config.
    """
    profile = config.profiles.get(service)
    if profile is False:
        return None  # auth-less service if `false` in config
    if not profile:
        profile = config.profiles.get("default")
    if not profile:
        raise ValueError(f"No profile has been defined for '{service}' in the config.")

    return profile


def get_cookie_jar(service, profile):
    """Get the profile's cookies if available."""
    cookie_file = os.path.join(directories.cookies, service, f"{profile}.txt")
    if os.path.isfile(cookie_file):
        cookie_jar = MozillaCookieJar(cookie_file)
        with open(cookie_file, "r+", encoding="utf-8") as fd:
            unescaped = html.unescape(fd.read())
            fd.seek(0)
            fd.truncate()
            fd.write(unescaped)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        return cookie_jar
    return None


def get_credentials(service, profile):
    """Get the profile's credentials if available."""
    cred = credentials.get(service, {}).get(profile)
    if cred:
        if isinstance(cred, list):
            return Credential(*cred)
        return Credential.loads(cred)
    return None


@click.group(name="dl", short_help="Download from a service.", cls=AliasedGroup, context_settings=dict(
    help_option_names=["-?", "-h", "--help"],
    max_content_width=116,  # max PEP8 line-width, -4 to adjust for initial indent
    default_map=config.arguments
))
@click.option("-p", "--profile", type=str, default=None,
              help="Profile to use when multiple profiles are defined for a service.")
@click.option("-q", "--quality", type=QUALITY, default=None,
              help="Download Resolution, defaults to best available.")
@click.option("-v", "--vcodec", type=click.Choice(["H264", "H265", "VP9", "AV1"], case_sensitive=False),
              default="H264",
              help="Video Codec, defaults to H264.")
@click.option("-a", "--acodec", type=click.Choice(["AAC", "AC3", "EC3", "VORB", "OPUS"], case_sensitive=False),
              default=None,
              help="Audio Codec")
@click.option("-r", "--range", "range_", type=click.Choice(["SDR", "HDR10", "HLG", "DV"], case_sensitive=False),
              default="SDR",
              help="Video Color Range, defaults to SDR.")
@click.option("-w", "--wanted", type=SEASON_RANGE, default=None,
              help="Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`, `S02-S02E03`, e.t.c, defaults to all.")
@click.option("-al", "--alang", type=LANGUAGE_RANGE, default=["orig"],
              help="Language wanted for audio.")
@click.option("-sl", "--slang", type=LANGUAGE_RANGE, default=["all"],
              help="Language wanted for subtitles.")
@click.option("--proxy", type=str, default=None,
              help="Proxy URI to use. If a 2-letter country is provided, it will try get a proxy from the config.")
@click.option("-A", "--audio-only", is_flag=True, default=False,
              help="Only download audio tracks.")
@click.option("-S", "--subs-only", is_flag=True, default=False,
              help="Only download subtitle tracks.")
@click.option("-C", "--chapters-only", is_flag=True, default=False,
              help="Only download chapters.")
@click.option("--list", "list_", is_flag=True, default=False,
              help="Skip downloading and list available tracks and what tracks would have been downloaded.")
@click.option("--keys", is_flag=True, default=False,
              help="Skip downloading, retrieve the decryption keys (via CDM or Key Vaults) and print them.")
@click.option("--cache", is_flag=True, default=False,
              help="Disable the use of the CDM and only retrieve decryption keys from Key Vaults. "
                   "If a needed key is unable to be retrieved from any Key Vaults, the title is skipped.")
@click.option("--no-cache", is_flag=True, default=False,
              help="Disable the use of Key Vaults and only retrieve decryption keys from the CDM.")
@click.option("--no-proxy", is_flag=True, default=False,
              help="Force disable all proxy use.")
@click.option("-nm", "--no-mux", is_flag=True, default=False,
              help="Do not mux the downloaded and decrypted tracks.")
@click.option("--mux", is_flag=True, default=False,
              help="Force muxing when using --audio-only/--subs-only/--chapters-only.")
@click.option("--log", "log_path", default=filenames.log,
              help="Log path (or filename). Path can contain the following f-string args: {name} {time}.")
@click.pass_context
def dl(ctx, profile, log_path, *_, **__):
    if not ctx.invoked_subcommand:
        raise ValueError("A subcommand to invoke was not specified, the main code cannot continue.")
    service = services.get_service_key(ctx.invoked_subcommand)

    log = get_logger(
        level=logging.DEBUG if ctx.parent.params["debug"] else logging.INFO,
        log_path=log_path
    )

    profile = profile or get_profile(service)
    service_config = get_service_config(service)
    vaults = Vaults(config.key_vaults, service=service)
    local_vaults = sum(v.vault_type == Vault.Types.LOCAL for v in vaults)
    remote_vaults = sum(v.vault_type == Vault.Types.REMOTE for v in vaults)
    log.info(f" + {local_vaults} Local Vault{'' if local_vaults == 1 else 's'}")
    log.info(f" + {remote_vaults} Remote Vault{'' if remote_vaults == 1 else 's'}")

    try:
        device = get_cdm(service, profile)
    except ValueError as e:
        log.exit(f" - {e}")
        raise
    log.info(f" + Loaded {device.__class__.__name__}: {device.system_id} (L{device.security_level})")
    cdm = Cdm(device)

    if profile:
        cookies = get_cookie_jar(service, profile)
        credentials = get_credentials(service, profile)
        if not cookies and not credentials and service_config.get("needs_auth", True):
            log.exit(f" - Profile {profile!r} has no cookies or credentials")
            raise
    else:
        cookies = None
        credentials = None

    ctx.obj = ContextData(
        config=service_config,
        vaults=vaults,
        cdm=cdm,
        profile=profile,
        cookies=cookies,
        credentials=credentials,
    )


@dl.result_callback()
@click.pass_context
def result(ctx, service, quality, range_, wanted, alang, slang, audio_only, subs_only, chapters_only, list_, keys,
           cache, no_cache, no_mux, mux, *_, **__):
    log = service.log

    service_name = service.__class__.__name__

    log.info("Retrieving Titles")
    titles = Titles(as_list(service.get_titles()))
    if not titles:
        log.exit(" - No titles returned!")
        raise
    titles.order()
    titles.print()

    for title in titles.with_wanted(wanted):
        if title.type == Title.Types.TV:
            log.info("Getting tracks for {title} S{season:02}E{episode:02} - {name}".format(
                title=title.name,
                season=title.season or 0,
                episode=title.episode or 0,
                name=title.episode_name
            ))
        else:
            log.info("Getting tracks for {title} ({year})".format(title=title.name, year=title.year or "???"))

        title.tracks.add(service.get_tracks(title), warn_only=True)
        title.tracks.add(service.get_chapters(title))
        title.tracks.sort_videos()
        title.tracks.sort_audios(by_language=alang)
        title.tracks.sort_subtitles(by_language=slang)
        title.tracks.sort_chapters()

        if not list(title.tracks):
            log.error(" - No tracks returned!")
            continue

        log.info("> All Tracks:")
        title.tracks.print()

        try:
            title.tracks.select_videos(by_quality=quality, by_range=range_, one_only=True)
            title.tracks.select_audios(by_language=alang, with_descriptive=False)
            title.tracks.select_subtitles(by_language=slang, with_forced=alang)
        except ValueError as e:
            log.error(f" - {e}")
            continue

        if audio_only or subs_only or chapters_only:
            title.tracks.videos.clear()
            if audio_only:
                if not subs_only:
                    title.tracks.subtitles.clear()
                if not chapters_only:
                    title.tracks.chapters.clear()
            elif subs_only:
                if not audio_only:
                    title.tracks.audios.clear()
                if not chapters_only:
                    title.tracks.chapters.clear()
            elif chapters_only:
                if not audio_only:
                    title.tracks.audios.clear()
                if not subs_only:
                    title.tracks.subtitles.clear()

            if not mux:
                no_mux = True

        log.info("> Selected Tracks:")
        title.tracks.print()

        if list_:
            continue  # only wanted to see what tracks were available and chosen

        skip_title = False
        for track in title.tracks:
            if not keys:
                log.info(f"Downloading: {track}")
            if track.encrypted:
                if not track.get_pssh(service.session):
                    log.exit(" - Failed to get PSSH")
                    raise
                log.info(f" + PSSH: {base64.b64encode(Box.build(track.pssh)).decode()}")
                if not track.get_kid(service.session):
                    log.exit(" - Failed to get KID")
                    raise
                log.info(f" + KID: {track.kid}")
            if not keys:
                if track.needs_proxy:
                    proxy = next(iter(service.session.proxies.values()), None)
                else:
                    proxy = None
                track.download(directories.temp, headers=service.session.headers, proxy=proxy)
                log.info(" + Downloaded")
                if isinstance(track, VideoTrack) and not title.tracks.subtitles:
                    log.info("Checking for EIA-608 closed captions in video stream")
                    cc = track.extract_c608()
                    if cc:
                        for c608 in cc:
                            title.tracks.add(c608)
                        log.info(f" + Found ({len(cc)}) EIA-608 closed caption{'' if len(cc) == 1 else 's'}")
            if track.encrypted:
                log.info("Decrypting...")
                if track.key:
                    log.info(f" + KEY: {track.key} (Static)")
                elif not no_cache:
                    track.key, vault_used = ctx.obj.vaults.get(track.kid, title.id)
                    if track.key:
                        log.info(f" + KEY: {track.key} (From {vault_used.name} {vault_used.vault_type} Key Vault)")
                        for vault in ctx.obj.vaults.vaults:
                            if vault == vault_used:
                                continue
                            if ctx.obj.vaults.insert_key(
                                vault, service_name.lower(), track.kid, track.key, title.id, commit=True
                            ):
                                log.info(f" + Cached to {vault.name} ({vault.vault_type}) vault")
                if not track.key:
                    if cache:
                        skip_title = True
                        break
                    session_id = ctx.obj.cdm.open(track.pssh)
                    ctx.obj.cdm.set_service_certificate(
                        session_id,
                        service.certificate(
                            challenge=ctx.obj.cdm.service_certificate_challenge,
                            title=title,
                            track=track,
                            session_id=session_id
                        ) or ctx.obj.cdm.common_privacy_cert
                    )
                    ctx.obj.cdm.parse_license(
                        session_id,
                        service.license(
                            challenge=ctx.obj.cdm.get_license_challenge(session_id),
                            title=title,
                            track=track,
                            session_id=session_id
                        )
                    )
                    content_keys = [
                        (x.kid.hex(), x.key.hex()) for x in ctx.obj.cdm.get_keys(session_id, content_only=True)
                    ]
                    if not content_keys:
                        log.exit(" - No content keys were returned by the CDM!")
                        raise
                    log.info(f" + Obtained content keys from the {ctx.obj.cdm.device.__class__.__name__} CDM")
                    for kid, key in content_keys:
                        if kid == "b770d5b4bb6b594daf985845aae9aa5f":
                            # Amazon HDCP test key
                            continue
                        log.info(f" + {kid}:{key}")
                    # cache keys into all key vaults
                    for vault in ctx.obj.vaults.vaults:
                        log.info(f"Caching to {vault.name} ({vault.vault_type}) vault")
                        cached = 0
                        for kid, key in content_keys:
                            if not ctx.obj.vaults.insert_key(vault, service_name.lower(), kid, key, title.id):
                                log.warning(f" - Failed, table {service_name.lower()} doesn't exist in the vault.")
                            else:
                                cached += 1
                        ctx.obj.vaults.commit(vault)
                        log.info(f" + Cached {cached}/{len(content_keys)} keys")
                        if cached < len(content_keys):
                            log.warning(f"    Failed to cache {len(content_keys) - cached} keys")
                    # use matching content key for the tracks key id
                    track.key = next((key for kid, key in content_keys if kid == track.kid), None)
                    if track.key:
                        log.info(f" + KEY: {track.key} (From CDM)")
                    else:
                        log.exit(f" - No content key with the key ID \"{track.kid}\" was returned")
                        raise
                if keys:
                    continue
                # TODO: Move decryption code to Track
                if not config.decrypter:
                    log.exit(" - No decrypter specified")
                    raise
                if config.decrypter == "packager":
                    platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)
                    names = ["shaka-packager", "packager", f"packager-{platform}"]
                    executable = next((x for x in (shutil.which(x) for x in names) if x), None)
                    if not executable:
                        log.exit(" - Unable to find packager binary")
                        raise
                    dec = os.path.splitext(track.locate())[0] + ".dec.mp4"
                    try:
                        os.makedirs(directories.temp, exist_ok=True)
                        subprocess.run([
                            executable,
                            "input={},stream={},output={}".format(
                                track.locate(),
                                track.__class__.__name__.lower().replace("track", ""),
                                dec
                            ),
                            "--enable_raw_key_decryption", "--keys",
                            ",".join([
                                f"label=0:key_id={track.kid.lower()}:key={track.key.lower()}",
                                # Apple TV+ needs this as shaka pulls the incorrect KID, idk why
                                f"label=1:key_id=00000000000000000000000000000000:key={track.key.lower()}",
                            ]),
                            "--temp_dir", directories.temp
                        ], check=True)
                    except subprocess.CalledProcessError:
                        log.exit(" - Failed!")
                        raise
                elif config.decrypter == "mp4decrypt":
                    executable = shutil.which("mp4decrypt")
                    if not executable:
                        log.exit(" - Unable to find mp4decrypt binary")
                        raise
                    dec = os.path.splitext(track.locate())[0] + ".dec.mp4"
                    try:
                        subprocess.run([
                            executable,
                            "--show-progress",
                            "--key", f"{track.kid.lower()}:{track.key.lower()}",
                            track.locate(),
                            dec,
                        ])
                    except subprocess.CalledProcessError:
                        log.exit(" - Failed!")
                        raise
                else:
                    log.exit(f" - Unsupported decrypter: {config.decrypter}")
                track.swap(dec)
                log.info(" + Decrypted")

            if keys:
                continue

            if track.needs_repack:
                log.info("Repackaging stream with FFmpeg (to fix malformed streams)")
                track.repackage()
                log.info(" + Repackaged")

            # TODO: This check seems to always return true, is there a way to determine
            # if the stream actually has EIA-608 captions without CCExtractor?
            if (isinstance(track, VideoTrack) and not title.tracks.subtitles and
                    any(x.get("codec_name", "").startswith("eia_")
                        for x in ffprobe(track.locate()).get("streams", []))):
                log.info("Extracting EIA-608 captions from stream with CCExtractor")
                track_id = f"ccextractor-{track.id}"
                # TODO: Is it possible to determine the language of EIA-608 captions?
                cc_lang = track.language
                try:
                    cc = track.ccextractor(
                        track_id=track_id,
                        out_path=filenames.subtitles.format(id=track_id, language_code=cc_lang),
                        language=cc_lang,
                        original=False
                    )
                except EnvironmentError:
                    log.warning(" - CCExtractor not found, cannot extract captions")
                else:
                    if cc:
                        title.tracks.add(cc)
                        log.info(" + Extracted")
                    else:
                        log.info(" + No captions found")
        if skip_title:
            continue
        if keys:
            continue
        if not list(title.tracks) and not title.tracks.chapters:
            continue
        # mux all final tracks to a single mkv file
        if no_mux:
            if title.tracks.chapters:
                final_file_path = directories.downloads
                if title.type == Title.Types.TV:
                    final_file_path = os.path.join(final_file_path, title.parse_filename(folder=True))
                os.makedirs(final_file_path, exist_ok=True)
                chapters_loc = filenames.chapters.format(filename=title.filename)
                title.tracks.export_chapters(chapters_loc)
                shutil.move(chapters_loc, os.path.join(final_file_path, os.path.basename(chapters_loc)))
            for track in title.tracks:
                media_info = MediaInfo.parse(track.locate())
                final_file_path = directories.downloads
                if title.type == Title.Types.TV:
                    final_file_path = os.path.join(
                        final_file_path, title.parse_filename(folder=True)
                    )
                os.makedirs(final_file_path, exist_ok=True)
                filename = title.parse_filename(media_info=media_info)
                extension = track.codec if isinstance(track, TextTrack) else os.path.splitext(track.locate())[1][1:]
                if isinstance(track, AudioTrack) and extension == "mp4":
                    extension = "m4a"
                track.move(os.path.join(final_file_path, f"{filename}.{track.id}.{extension}"))
        else:
            log.info("Muxing tracks into an MKV container")
            muxed_location, returncode = title.tracks.mux(title.filename)
            if returncode == 1:
                log.warning(" - mkvmerge had at least one warning, will continue anyway...")
            elif returncode >= 2:
                log.exit(" - Failed to mux tracks into MKV file")
                raise
            log.info(" + Muxed")
            for track in title.tracks:
                track.delete()
            if title.tracks.chapters:
                try:
                    os.unlink(filenames.chapters.format(filename=title.filename))
                except FileNotFoundError:
                    pass
            media_info = MediaInfo.parse(muxed_location)
            final_file_path = directories.downloads
            if title.type == Title.Types.TV:
                final_file_path = os.path.join(
                    final_file_path, title.parse_filename(media_info=media_info, folder=True)
                )
            os.makedirs(final_file_path, exist_ok=True)
            # rename muxed mkv file with new data from mediainfo data of it
            if audio_only:
                extension = "mka"
            elif subs_only:
                extension = "mks"
            else:
                extension = "mkv"
            shutil.move(
                muxed_location,
                os.path.join(final_file_path, f"{title.parse_filename(media_info=media_info)}.{extension}")
            )

    log.info("Processed all titles!")


def load_services():
    for service in services.__dict__.values():
        if callable(getattr(service, "cli", None)):
            dl.add_command(service.cli)


load_services()
