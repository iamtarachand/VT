import json
import os
from http.cookiejar import MozillaCookieJar

import click
from langcodes import Language

from vinetrimmer.config import directories
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Filmio(BaseService):
    """
    Service code for Filmio (https://www.filmio.hu/).

    \b
    Authorization: Credentials or Cookies
    Security: UHD@-- FHD@L3

    Note: The service currently uses a static key to encrypt all content.
    """

    ALIASES = ["FMIO", "filmio"]
    GEOFENCE = ["hu"]

    @staticmethod
    @click.command(name="Filmio", short_help="https://filmio.hu")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Filmio(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

        self.profile = ctx.obj.profile

        self.configure()

    def get_titles(self):
        r = self.session.get(
            self.config["endpoints"]["metadata"].format(title_id=self.title)
        )
        res = r.json()

        if res.get("status") == 401:
            self.log.warning(" - Cookies expired, logging in again...")
            os.unlink(self.cookie_file)
            self.configure()
            return self.get_titles()

        if "error" in res:
            self.log.exit(f" - Failed to get manifest: {res['message']} [{res['status']}]")
            raise

        if res["visibilityDetails"] != "OK":
            self.log.exit(f" - This title is not available. [{res['visibilityDetails']}]")
            raise

        return [Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=res["name"],
            year=res["title"]["year"],
            source=self.ALIASES[0],
            service_data=r
        )]

    def get_tracks(self, title):
        res = title.service_data.json()
        mpd_url = res["movie"]["contentUrl"]

        tracks = Tracks.from_mpd(
            data=self.session.get(mpd_url).text,
            url=mpd_url,
            lang=Language.get(next(iter(res["originalLanguages"][0]))),
            source=self.ALIASES[0]
        )

        for track in tracks:
            track.needs_proxy = True
            track.get_kid()
            if track.kid == "6761374a7eb04b59a595a943f4dbcdbe":
                track.key = "ed38695f26825877db9b0335f2212bb9"

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, *, challenge, title, **_):
        r = self.session.post(self.config["endpoints"]["license"], params={
            "drmToken": title.service_data.headers["drmtoken"]
        }, data=challenge)

        try:
            res = r.json()
        except json.JSONDecodeError:
            # Not valid JSON, so probably an actual license
            return r.content
        else:
            self.log.exit(f" - Failed to get license: {res['message']} [{res['status']}]")

    # Service-specific functions

    def configure(self):
        self.cookie_file = os.path.join(directories.cookies, self.__class__.__name__, f"{self.profile}.txt")
        cookie_jar = MozillaCookieJar(self.cookie_file)

        if os.path.isfile(self.cookie_file):
            cookie_jar.load()
            self.session.cookies.update(cookie_jar)
            self.log.info(" + Using saved cookies")
            return

        self.log.info(" + Logging in")
        if not (self.credentials and self.credentials.username and self.credentials.password):
            self.log.exit(" - No credentials provided, unable to log in.")
            raise

        res = self.session.post(self.config["endpoints"]["login"], data={
            "username": self.credentials.username,
            "password": self.credentials.password
        }).json()

        if "error" in res:
            self.log.exit(f" - Failed to log in: {res['message']} [{res['status']}]")
            raise

        for cookie in self.session.cookies:
            cookie_jar.set_cookie(cookie)
        os.makedirs(os.path.dirname(self.cookie_file, exist_ok=True))
        cookie_jar.save(ignore_discard=True)
