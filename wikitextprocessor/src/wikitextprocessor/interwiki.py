import logging
import time
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .core import Wtp


logger = logging.getLogger(__name__)
INTERWIKI_REQUEST_ATTEMPTS = 3
INTERWIKI_REQUEST_TIMEOUT = 30


def get_interwiki_data(wtp: "Wtp") -> list[dict[str, Union[str, bool]]]:
    import requests

    from .wikidata import get_user_agent

    request_kwargs = {
        "params": {  # type: ignore
            "action": "query",
            "meta": "siteinfo",
            "siprop": "interwikimap",
            "format": "json",
            "formatversion": 2,
        },
        "headers": {"user-agent": get_user_agent()},
        "timeout": INTERWIKI_REQUEST_TIMEOUT,
    }
    url = f"https://{wtp.lang_code}.{wtp.project}.org/w/api.php"

    for attempt in range(INTERWIKI_REQUEST_ATTEMPTS):
        try:
            response = requests.get(url, **request_kwargs)
        except requests.RequestException as error:
            if attempt == INTERWIKI_REQUEST_ATTEMPTS - 1:
                logger.error(
                    "Interwiki request failed after %s attempts; "
                    "continuing with an empty map: %s",
                    INTERWIKI_REQUEST_ATTEMPTS,
                    error,
                )
                return []
            delay = 2**attempt
            logger.warning(
                "Interwiki request failed; retrying in %s second(s) (%s/%s)",
                delay,
                attempt + 1,
                INTERWIKI_REQUEST_ATTEMPTS,
            )
            time.sleep(delay)
            continue

        if response.ok:
            results = response.json()
            return results.get("query", {}).get("interwikimap", [])
        return []

    return []


def init_interwiki_map(wtp: "Wtp") -> None:
    wtp.db_conn.execute(
        """
    CREATE TABLE IF NOT EXISTS interwiki_maps (
    prefix TEXT PRIMARY KEY,
    url TEXT,
    protorel INTEGER,
    local INTEGER)
    """
    )
    if len(get_interwiki_map(wtp)) == 0:
        for result in get_interwiki_data(wtp):
            wtp.db_conn.execute(
                "INSERT INTO interwiki_maps VALUES(?, ?, ?, ?)",
                (
                    result["prefix"],
                    result["url"],
                    result.get("protorel", False),
                    result.get("local", False),
                ),
            )
        wtp.db_conn.commit()


def get_interwiki_map(wtp: "Wtp") -> dict[str, dict[str, Union[str, bool]]]:
    return {
        prefix: {
            "prefix": prefix,
            "url": url if not protorel else url.removeprefix("https:"),
            "isProtocolRelative": bool(protorel),
            "isLocal": bool(local),
            "isCurrentWiki": url.startswith(
                f"https://{wtp.lang_code}.{wtp.project}.org"
            ),
            "isTranscludable": False,
            "isExtraLanguageLink": False,
        }
        for (prefix, url, protorel, local) in wtp.db_conn.execute(
            "SELECT * FROM interwiki_maps"
        )
    }


def mw_site_interwikiMap(wtp, filter_arg=None):
    # https://www.mediawiki.org/wiki/Manual:Interwiki
    # https://www.mediawiki.org/wiki/Extension:Scribunto/Lua_reference_manual#mw.site.interwikiMap
    interwiki_map = {}
    for key, value in get_interwiki_map(wtp).items():
        if (
            filter_arg is None
            or (filter_arg == "local" and value["isLocal"])
            or (filter_arg == "!local" and not value["isLocal"])
        ):
            interwiki_map[key] = wtp.lua.table_from(value)

    return wtp.lua.table_from(interwiki_map)
