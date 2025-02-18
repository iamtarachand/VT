import base64
import json
import os
import posixpath
import re
import threading
import time
import urllib.parse
from datetime import datetime
from hashlib import md5

import click
import websocket
from langcodes import Language

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Vudu(BaseService):
    """
    Service code for Vudu (https://www.vudu.com/).

    \b
    Authorization: Credentials
    Security: UHD@L1* HD@L1 SD@L3

    *HEVC/UHD requires a whitelisted CDM.
    """

    ALIASES = ["VUDU"]
    GEOFENCE = ["us"]

    VIDEO_QUALITY_MAP = {
        "HD": "hdx",
    }

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Vudu", short_help="https://vudu.com")
    @click.argument("title", type=str)
    @click.option("-q", "--quality", default=None,
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Vudu(ctx, **kwargs)

    def __init__(self, ctx, title, quality):
        self.title = title
        self.quality = quality
        super().__init__(ctx)

        self.profile = ctx.obj.profile

        self.proxy = ctx.parent.params["proxy"]
        self.range = ctx.parent.params["range_"]
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]

        if (ctx.parent.params.get("quality") or 0) > 1080 and self.quality != "UHD":
            self.log.info(" + Switched manifest quality to UHD to be able to get 2160p video track")
            self.quality = "UHD"

        if self.vcodec == "H265" and self.quality != "UHD":
            self.log.info(" + Switched manifest quality to UHD to be able to get H265 manifest")
            self.quality = "UHD"

        if self.range in ("HDR10", "DV") and self.quality != "UHD":
            self.log.info(f" + Switched manifest quality to UHD to be able to get {self.range} dynamic range")
            self.quality = "UHD"

        if self.quality == "UHD" and self.vcodec != "H265":
            self.log.info(" + Switched video codec to H265 to be able to get UHD manifest")
            self.vcodec = "H265"

        self.user_id = None
        self.session_key = None
        self.websocket = None
        self.keepalive_thread = None

        self.configure()

    def get_titles(self):
        res = self.extract_json(self.session.get(self.config["endpoints"]["cache"], params={
            "_type": "contentSearch",
            "contentEncoding": "gzip",
            "contentId": self.title,
            "dimensionality": "any",
            "followup": [
                "ultraVioletability", "longCredits", "usefulTvPreviousAndNext", "superType",
                "episodeNumberInSeason", "advertContentDefinitions", "tag", "hasBonusWithTagExtras",
                "subtitleTrack", "ratingsSummaries", "geneGenres", "seasonNumber", "trailerEditionId", "genres",
                "usefulStreamableOffers", "walmartOffers", "preOrderOffers", "editions", "promoTags",
                "advertEnabled", "uxPromoTags"
            ],
            "format": "application/json"
        }))
        self.log.debug(json.dumps(res, indent=4))
        if "content" not in res:
            self.log.exit(" - Title not found")
            raise

        content_type = res["content"][0]["type"][0]
        title = res["content"][0]["title"][0]
        season_ids = []
        contents = []

        if content_type == "program":
            return [Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["content"][0]["title"][0],
                year=res["content"][0]["releaseTime"][0].split("-")[0],
                source=self.ALIASES[0],
                service_data=res["content"][0]
            )]
        else:
            # TODO: Figure out a better way to get series titles without extra things at the end
            if content_type == "series":
                title = re.sub(r" \[TV Series]$", "", title)
                res = self.extract_json(self.session.get(self.config["endpoints"]["cache"], params={
                    "_type": "contentSearch",
                    "contentEncoding": "gzip",
                    "count": "75",
                    "dimensionality": "any",
                    "followup": ["seasonNumber", "promoTags", "ratingsSummaries", "advertEnabled", "uxPromoTags"],
                    "format": "application/json",
                    "includeComingSoon": "true",
                    "listType": "useful",
                    "offset": "0",
                    "seriesId": self.title,
                    "sortBy": "-seasonNumber",
                    "type": "season"
                }))
                self.log.debug(json.dumps(res, indent=4))
                if "content" not in res:
                    self.log.exit(" - Title not found")
                    raise
                season_ids = [x["contentId"][0] for x in res["content"]]
            elif content_type == "season":
                title = re.sub(r": Season \d+$", "", title)
                season_ids = [self.title]
            elif content_type == "episode":
                title = re.sub(r": .+", "", title)
                contents += res["content"]

            for season_id in season_ids:
                res = self.extract_json(self.session.get(self.config["endpoints"]["cache"], params={
                    "_type": "contentSearch",
                    "contentEncoding": "gzip",
                    "count": "75",
                    "dimensionality": "any",
                    "followup": [
                        "usefulStreamableOffers", "episodeNumberInSeason", "mpaaRating", "subtitleTrack", "editions",
                        "seasonNumber", "promoTags", "ratingsSummaries", "advertEnabled", "uxPromoTags"
                    ],
                    "format": "application/json",
                    "includeComingSoon": "true",
                    "listType": "useful",
                    "offset": "0",
                    "seasonId": season_id,
                    "sortBy": "episodeNumberInSeason"
                }))
                self.log.debug(json.dumps(res, indent=4))
                if "content" not in res:
                    self.log.exit(" - Title not found")
                    raise
                contents += res["content"]

            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=title,
                season=int(x["seasonNumber"][0]),
                episode=int(x["episodeNumberInSeason"][0]),
                # TODO: Figure out a better way to get the unprefixed episode name.
                # Episode name often/always(?) starts with the show name, but it's not always an exact match.
                episode_name=re.sub(r"^.+?: ", "", x["title"][0]),
                source=self.ALIASES[0],
                service_data=x
            ) for x in contents]

    def get_tracks(self, title):
        tracks = Tracks()

        if self.quality is None:
            try:
                variant = [
                    x for x in title.service_data["contentVariants"][0]["contentVariant"] if "dashEditionId" in x
                ][-1]
            except IndexError:
                self.log.exit(" - No DASH streams found")
                raise
        else:
            variant = next((
                x for x in title.service_data["contentVariants"][0]["contentVariant"]
                if x["videoQuality"][0] == self.VIDEO_QUALITY_MAP.get(self.quality, self.quality).lower()
            ), None)
            if not variant:
                self.log.exit(" - Requested quality not available")
                raise

        if self.vcodec == "H265":
            if self.range == "SDR":
                video_profile = "main10"
            elif self.range == "HDR10":
                video_profile = "hdr10"
            elif self.range == "DV":
                video_profile = "dvheStn"
        else:
            video_profile = "highP"

        edition = next((
            x for x in variant["editions"][0]["edition"]
            if x["editionFormat"][0] == "dash" and video_profile in x["videoProfile"]
        ), None)
        if not edition:
            self.log.exit(" - Requested edition not found")

        edition_format = edition["editionFormat"][0]

        edition_id = edition["editionId"][0]
        title.service_data["edition_id"] = edition_id

        res = self.websocket_send({
            "_type": "editionLocationGet",
            "editionFormat": edition_format,
            "editionId": edition_id,
            "isSecure": "true",
            "requestCallbackId": 1,
            "userId": self.user_id,
            "videoProfile": video_profile,
        })
        if res["_type"] == ["error"]:
            self.log.exit(f" - Failed to get manifest: {res['text'][0]}")
            raise
        mpd_url = posixpath.join(res["location.0.baseUri"][0], "manifest.mpd" + res["location.0.uriSuffix"][0])
        self.log.debug(mpd_url)

        tracks = Tracks.from_mpd(
            url=mpd_url,
            lang=Language.find(title.service_data["language"][0]),
            source=self.ALIASES[0]
        )

        if res["location.0.dynamicRange"] == ["hdr10"]:
            for video in tracks.videos:
                video.hdr10 = True

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        for sub in title.service_data["subtitleTrack"]:
            url = posixpath.join(
                res["location.0.subtitleBaseUri"][0], f"subtitle.{sub['version'][0]}.{sub['languageCode'][0]}.vtt"
            )
            tracks.add(TextTrack(
                id_=md5(url.encode()).hexdigest()[0:6],
                source=self.ALIASES[0],
                url=url,
                # metadata
                codec="vtt",
                language=sub["languageCode"][0]
            ))

        self.keepalive_thread = threading.Thread(target=self.websocket_keep_alive)
        self.keepalive_thread.daemon = True
        self.keepalive_thread.start()

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return self.config["certificate"]

    def license(self, *, challenge, title, **_):
        self.keepalive_thread = None  # Signal the thread to stop

        res = self.websocket_send({
            "_type": "widevineDrmLicenseRequest",
            "drmToken": base64.b64encode(challenge).decode(),
            "editionId": title.service_data["edition_id"],
            "requestCallbackId": 3,
            "userId": self.user_id,
        })
        if res["status"] != ["ok"]:
            self.log.exit(f" - License request failed: {res['status'][0]}")
            raise
        return res["license"][0]

    # Service-specific functions

    @staticmethod
    def extract_json(res):
        return json.loads(res.text.replace("/*-secure-", "").replace("*/", ""))

    def websocket_send(self, params):
        self.log.debug(f"<< {params}")
        self.websocket.send(urllib.parse.urlencode(params))
        res = urllib.parse.parse_qs(self.websocket.recv())
        self.log.debug(f">> {res}")
        return res

    def websocket_keep_alive(self):
        while self.keepalive_thread:
            res = self.websocket_send({"_type": "keepAliveRequest"})
            if res["_type"] != ["keepAliveResponse"]:
                raise ValueError("Did not receive keepAliveResponse from WebSocket")
            time.sleep(30)

    def get_session_keys(self):
        cache_path = self.get_cache(f"session_keys_{self.profile}.json")
        if os.path.isfile(cache_path):
            with open(cache_path, encoding="utf-8") as fd:
                session_keys = json.load(fd)
            if datetime.strptime(session_keys["expirationTime"][0], "%Y-%m-%d %H:%M:%S.%f") > datetime.utcnow():
                self.log.info(" + Using cached session keys")
                return session_keys

        self.log.info(" + Logging in")
        res = self.extract_json(self.session.post(self.config["endpoints"]["api"], data={
            "contentType": "application/x-vudu-url-note",
            "query": urllib.parse.urlencode({
                "claimedAppId": "appleTv::vudu",
                "format": "application/json",
                "_type": "sessionKeyRequest",
                "contentEncoding": "gzip",
                "followup": "user",
                "password": self.credentials.password,
                "userName": self.credentials.username,
                "weakSeconds": 25920000,
                "sensorData": ""
            })
        }))
        self.log.debug(res)
        if res["status"] != ["success"]:
            self.log.exit(f" - Login failed: {res['status'][0]}")
            raise
        session_keys = res["sessionKey"][0]

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fd:
            json.dump(session_keys, fd)

        return session_keys

    def configure(self):
        session_keys = self.get_session_keys()
        self.user_id = session_keys["user"][0]["userId"][0]
        self.session_key = session_keys["sessionKey"][0]

        self.log.info(" + Opening WebSocket connection")
        proxy_ = self.get_proxy(self.proxy or self.GEOFENCE[0])
        proxy = urllib.parse.urlparse(proxy_) if proxy_ else None

        self.websocket = websocket.create_connection(
            self.config["endpoints"]["websocket"],
            http_proxy_host=proxy.hostname if proxy else None,
            http_proxy_port=proxy.port if proxy else None,
            http_proxy_auth=(
                urllib.parse.unquote(proxy.username) if proxy.username else None,
                urllib.parse.unquote(proxy.password) if proxy.password else None
            ) if proxy else None
        )

        self.log.info(" + Authenticating with session keys")
        res = self.websocket_send({
            "_type": "lightDeviceLoginQuery",
            "accountId": self.user_id,
            "lightDeviceId": 1,
            "lightDeviceKey": "Ad111ec153899d144d81163dab6a3914a5520bfc082a0f02fce8c3498568939ab",
            "sessionKey": self.session_key
        })
        if res["status"] != ["ok"]:
            self.log.exit(" - WebSocket authentication failed: {res['errorDescription']}")
            raise
