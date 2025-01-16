import uuid

import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class DiscoveryPlus(BaseService):
    """
    Service code for Discovery+ (https://www.discoveryplus.com/).

    \b
    Authorization: Cookies
    Security: UHD@? FHD@L3
    """

    ALIASES = ["DSCP", "discoveryplus", "discovery+"]
    GEOFENCE = ["us"]

    @staticmethod
    @click.command(name="DiscoveryPlus", short_help="https://discoveryplus.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return DiscoveryPlus(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

        self.configure()

    def get_titles(self):
        r = self.session.get(self.config["endpoints"]["show"].format(title_id=self.title))
        if not r.ok:
            self.log.exit(f" - HTTP Error {r.status_code}: {r.reason}")
            raise
        res = r.json()

        page = next(x for x in res["included"] if x["type"] == "page")
        episodes = [x for x in res["included"] if x["type"] == "video"]

        return [Title(
            id_=ep["id"],
            type_=Title.Types.MOVIE if ep["attributes"]["videoType"] == "STANDALONE" else Title.Types.TV,
            name=page["attributes"]["title"],
            year=ep["attributes"]["airDate"][:4],
            season=ep["attributes"].get("seasonNumber"),
            episode=ep["attributes"].get("episodeNumber"),
            episode_name=ep["attributes"]["name"],
            source=self.ALIASES[0],
            service_data=ep,
        ) for ep in episodes if ep["attributes"]["videoType"] != "CLIP"]

    def get_tracks(self, title):
        r = self.session.post(self.config["endpoints"]["video_playback_info"], json={
            "deviceInfo": {
                "adBlocker": False,
                "drmSupported": True,
                "hdrCapabilities": ["SDR"],
                "hwDecodingCapabilities": [],
                "player": {
                    "width": 897,
                    "height": 505,
                },
                "screen": {
                    "width": 1536,
                    "height": 864,
                },
                "soundCapabilities": ["STEREO"],
            },
            "videoId": title.id,
            "wisteriaProperties": {
                "advertiser": {
                    "adId": (
                        "|90805886454030367517733395486106519740|7|162946912055798e598aa6afa21334181500f4dd59d91",
                    ),
                    "firstPlay": 0,
                    "fwDid": "",
                    "fwIsLat": 1,
                    "fwNielsenAppId": "P5A0FD4DE-4AE6-4B22-811B-36B9BD091980",
                    "gpaln": "",
                    "interactiveCapabilities": ["brightline"],
                },
                "appBundle": "",
                "device": {
                    "browser": {
                        "name": "chrome",
                        "version": "92.0.4515.131",
                    },
                    "language": "en",
                    "make": "",
                    "model": "",
                    "name": "Chrome",
                    "os": "Windows",
                    "osVersion": "10",
                    "type": "desktop",
                    "id": "5623aa854dfd48ba8d238067d11ebc55",
                    "player": {
                        "name": "Discovery Player Web",
                        "version": "25.3.2",
                    },
                },
                "gdpr": 0,
                "siteId": "dplus_us",
                "platform": "desktop",
                "playbackId": str(uuid.uuid4()),
                "product": "dplus_us",
                "sessionId": str(uuid.uuid4()),
                "streamProvider": {
                    "suspendBeaconing": 0,
                    "hlsVersion": 7,
                    "pingConfig": 0,
                    "version": "1.0.0",
                },
            },
        })
        if not r.ok:
            self.log.exit(f" - HTTP Error {r.status_code}: {r.reason}")
            raise
        res = r.json()

        tracks = Tracks.from_mpd(
            url=res["data"]["attributes"]["streaming"][0]["url"],
            lang="en",  # TODO: Don't assume
            source=self.ALIASES[0],
        )

        # Remove subtitles from MPD, those are segmented VTT which we can't handle yet.
        # The streams have CC that can be extracted using CCExtractor.
        tracks.subtitles.clear()

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, *, challenge, **_):
        r = self.session.post(self.config["endpoints"]["license"], data=challenge)
        if not r.ok:
            self.log.exit(f" - HTTP Error {r.status_code}: {r.reason}")
        return r.content

    # Service-specific functions

    def configure(self):
        self.session.headers.update({
            "origin": "https://www.discoveryplus.com",
            "referer": "https://www.discoveryplus.com/",
            "x-disco-client": "WEB:UNKNOWN:dplus_us:1.15.0",
            "x-disco-params": "realm=go,siteLookupKey=dplus_us,features=ar",
        })
