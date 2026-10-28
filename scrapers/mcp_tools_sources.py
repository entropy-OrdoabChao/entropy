"""
PHASE_2A_MCP_SOURCE_FETCH
Single MCP tool for all primary source scrapers.

Tool registered:
  beezle.source_fetch  — fetch from any primary source (CL, PA, USC, CFR, FRCP)
"""
from __future__ import annotations
from typing import Optional


def register_source_fetch_tools(mcp) -> int:
    count = 0

    @mcp.tool
    def source_fetch(
        source: str,
        params: Optional[dict] = None,
    ) -> dict:
        """Fetch primary legal sources. One tool, all scrapers.

        source: 'courtlistener' | 'pa_legis' | 'pa_code' | 'cfr' | 'usc' | 'frcp'

        params by source:
          courtlistener: {action:'search'|'opinion'|'search_and_save', query, court?, n?, opinion_id?}
          pa_legis:      {title:int, section?}
          pa_code:       {title:int, chapter?}
          cfr:           {title:int, part?, section?}
          usc:           {title:int, section?}
          frcp:          {rule?}

        Returns {ok:bool, result|results|error}.
        """
        params = params or {}
        src = source.lower().strip()
        try:
            if src == "courtlistener":
                from scrapers.courtlistener import search_opinions, fetch_opinion, search_and_save
                action = params.get("action", "search")
                if action == "search":
                    return {"ok": True, "results": search_opinions(params.get("query",""), params.get("court",""), params.get("n",10))}
                elif action == "opinion":
                    return {"ok": True, "result": fetch_opinion(int(params["opinion_id"]), save=params.get("save",True))}
                elif action == "search_and_save":
                    return {"ok": True, "results": search_and_save(params.get("query",""), params.get("court",""), params.get("n",5))}
                return {"ok": False, "error": f"Unknown CL action: {action}"}
            elif src == "pa_legis":
                from scrapers.pa_legis import fetch_title
                return {"ok": True, "result": fetch_title(int(params["title"]), params.get("section"))}
            elif src == "pa_code":
                from scrapers.pa_code import fetch_title as fetch_pa_code
                return {"ok": True, "result": fetch_pa_code(int(params["title"]), params.get("chapter"))}
            elif src == "cfr":
                from scrapers.fed_register import fetch_cfr
                return {"ok": True, "result": fetch_cfr(int(params["title"]), params.get("part"), params.get("section"))}
            elif src == "usc":
                from scrapers.fed_register import fetch_usc
                return {"ok": True, "result": fetch_usc(int(params["title"]), params.get("section"))}
            elif src == "frcp":
                from scrapers.fed_register import fetch_frcp
                return {"ok": True, "result": fetch_frcp(params.get("rule"))}
            else:
                return {"ok": False, "error": f"Unknown source: {source}. Valid: courtlistener, pa_legis, pa_code, cfr, usc, frcp"}
        except NotImplementedError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    count += 1

    return count