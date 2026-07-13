"""Seed the food-security corpus for the El Nino demo (A4, demo scope).

Kenya + Zambia maize under El Nino, three periods: current 2026-27 event,
2023-24 analog, 2015-16 analog. URLs verified by the 2026-07-13 corpus research
(docs/food-security/corpus-blueprint.md). Failures log and skip — never abort.

Run:  PYTHONPATH=apps/api/src uv run python -m app.food_security.seed_corpus
"""

from __future__ import annotations

import sys
import time

import requests

from ..rag import docloader
from ..rag.store import Corpus

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CONSENSUS, AGENCY = "multi-agency-consensus", "single-agency"
KE, ZM, BOTH = ["Kenya"], ["Zambia"], ["Kenya", "Zambia"]
NOW, A23, A15 = "el-nino-2026-27", "el-nino-2023-24", "el-nino-2015-16"


def doc(url, source, title, pub_date, temporal, validation, event=None,
        countries=None, crops=None, doc_type=None, filename=None):
    meta = {"source": source, "title": title, "pub_date": pub_date,
            "temporal": temporal, "validation": validation, "url": url}
    for k, v in (("event", event), ("countries", countries), ("crops", crops),
                 ("doc_type", doc_type)):
        if v:
            meta[k] = v
    return {"url": url, "filename": filename, "metadata": meta}


def cpc_discussions():
    windows = [(NOW, ["jan2026", "feb2026", "mar2026", "apr2026", "may2026", "jun2026", "jul2026"]),
               (A23, ["jun2023", "aug2023", "oct2023", "dec2023", "feb2024", "apr2024"]),
               (A15, ["jun2015", "aug2015", "oct2015", "dec2015", "feb2016", "apr2016"])]
    out = []
    for event, months in windows:
        for m in months:
            out.append(doc(
                f"https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_disc_{m}/ensodisc.pdf",
                "NOAA CPC", f"ENSO Diagnostic Discussion {m[:3].title()} {m[3:]}",
                f"{m[3:]}-{'%02d' % ('janfebmaraprmayjunjulaugsepoctnovdec'.index(m[:3]) // 3 + 1)}",
                "forecast", AGENCY, event=event, doc_type="enso-status"))
    return out


SOURCES = cpc_discussions() + [
    # ---- current event: signal + forecasts -------------------------------
    doc("https://www.uncclearn.org/wp-content/uploads/library/WMO-El-Nino-Feb-2026_en.pdf",
        "WMO", "El Nino/La Nina Update Feb 2026", "2026-02", "forecast", CONSENSUS, NOW,
        doc_type="enso-status"),
    doc("https://www.icpac.net/documents/1076/Technical_Statement_from_the_73rd_Greater_Horn_of_Africa_Climate_Outlook_Forum_GnOzTFx.pdf",
        "ICPAC GHACOF", "GHACOF-73 statement (JJAS 2026)", "2026-05", "forecast", CONSENSUS, NOW, KE),
    doc("https://www.icpac.net/documents/1032/GHACOF72_Technical_Statement.pdf",
        "ICPAC GHACOF", "GHACOF-72 statement (MAM 2026)", "2026-02", "forecast", CONSENSUS, NOW, KE),
    doc("https://www.sadc.int/sites/default/files/2026-01/FINAL%20SARCOF-32%20STATEMENT.pdf",
        "SADC SARCOF", "SARCOF-32 statement (2025/26 season — pre-El Nino run-up)",
        "2026-01", "forecast", CONSENSUS, countries=ZM, doc_type="pre-event-baseline"),
    doc("https://meteo.go.ke/documents/3796/June-July-August_JJA_2026_Seasonal_Forecast.pdf",
        "Kenya Meteorological Department", "JJA 2026 seasonal forecast", "2026-05", "forecast",
        AGENCY, NOW, KE),
    doc("https://zmd.gov.zm/documents/8/2025-2026_SEASON_RAINFALL_FORECAST_FOR_ZAMBIA_1.pdf",
        "Zambia Meteorological Department",
        "2025/26 season rainfall forecast (pre-El Nino run-up)", "2025-09", "forecast",
        AGENCY, countries=ZM, doc_type="pre-event-baseline"),
    # ---- current event: conditions + synthesis ---------------------------
    doc("https://www.cropmonitor.org/s/CM_Special_Report_El_Nino_2026-27.pdf",
        "GEOGLAM Crop Monitor", "Special Report: El Nino 2026-27", "2026-06", "forecast",
        CONSENSUS, NOW, BOTH, ["maize"]),
    doc("https://www.cropmonitor.org/s/EarlyWarning_CropMonitor_202506.pdf",
        "GEOGLAM Crop Monitor", "Crop Monitor for Early Warning Jun 2025", "2025-06",
        "retrospective", CONSENSUS, countries=BOTH, crops=["maize"],
        doc_type="pre-event-baseline"),
    doc("https://www.cropmonitor.org/s/AMIS_CropMonitor_202506.pdf",
        "GEOGLAM Crop Monitor", "Crop Monitor for AMIS Jun 2025", "2025-06",
        "retrospective", CONSENSUS, crops=["maize"], doc_type="pre-event-baseline"),
    # Mutable URLs (overwritten in place upstream): pub_date is the snapshot's
    # "Reference Date"/issue — re-verify on any re-run; parse-at-ingest lands with full A4.
    doc("https://www.fao.org/giews/countrybrief/country/KEN/pdf/KEN.pdf",
        "FAO GIEWS", "Kenya country brief", "2026-05", "retrospective", AGENCY, NOW, KE,
        ["maize"]),
    doc("https://www.fao.org/giews/countrybrief/country/ZMB/pdf/ZMB.pdf",
        "FAO GIEWS", "Zambia country brief", "2026-04", "retrospective", AGENCY, NOW, ZM,
        ["maize"]),
    doc("https://knowledgeweb.ndma.go.ke/Content/LibraryDocuments/National_DEW_Bulletin_January_202620260217011002.pdf",
        "Kenya NDMA", "National drought early-warning bulletin Jan 2026", "2026-01",
        "retrospective", AGENCY, NOW, KE),
    doc("https://www.icpac.net/documents/1047/FSNWG_Statement_January-February_2026.pdf",
        "IGAD FSNWG", "Food security & nutrition statement Jan-Feb 2026", "2026-01",
        "retrospective", CONSENSUS, NOW, KE),
    doc("https://reliefweb.int/attachments/e74d8661-b9d3-4ef7-a138-1200fd7b6015/WFP-0000174269.pdf",
        "WFP", "El Nino impacts on food security: lessons for 2026/27 preparedness",
        "2026-06", "retrospective", AGENCY, NOW, BOTH, ["maize"]),
    doc("https://www.welthungerhilfe.org/fileadmin/pictures/publications/en/fact-sheets/topics/2026-june-el-nino-potential-implications-briefing-whh.pdf",
        "Welthungerhilfe", "El Nino potential implications briefing Jun 2026", "2026-06",
        "forecast", AGENCY, NOW),
    # ---- market/baseline context ------------------------------------------
    doc("https://storage.googleapis.com/amis-9189b-strapi/AMIS_Market_Monitor_current_553d2f1113/AMIS_Market_Monitor_current_553d2f1113.pdf",
        "AMIS", "Market Monitor No. 140 (Jul 2026 snapshot; mutable URL)", "2026-07",
        "retrospective", CONSENSUS, NOW, crops=["maize"], doc_type="market"),
    doc("https://eagc.org/wp-content/uploads/2025/01/GW-Q3-2024-final-.pdf",
        "EAGC", "Grain Watch Q3 2024", "2024-10", "retrospective", AGENCY, A23, KE,
        ["maize"], doc_type="market"),
    doc("https://www.knbs.or.ke/wp-content/uploads/2025/10/National-Agriculture-Production-Report-2025.pdf",
        "Kenya National Bureau of Statistics", "National agriculture production report 2025",
        "2025-10", "retrospective", AGENCY, countries=KE, crops=["maize"], doc_type="baseline"),
    doc("https://www.zamstats.gov.zm/wp-content/uploads/2025/09/Zambia-Food-Balance-Sheet-Report-2019-2023.pdf",
        "ZamStats", "Zambia food balance sheet 2019-2023", "2025-09", "retrospective", AGENCY,
        countries=ZM, crops=["maize"], doc_type="baseline"),
    # ---- trust layer -------------------------------------------------------
    doc("https://centaur.reading.ac.uk/91641/3/EastAfrica_seasonal_forecasts_resubmission4_clean.pdf",
        "Univ. Reading (Weather & Forecasting 35(5))",
        "Skill of East-African seasonal rainfall forecasts", "2020-09", "retrospective",
        "peer-reviewed", countries=KE, doc_type="forecast-skill"),
    # ---- 2023-24 analog -----------------------------------------------------
    doc("https://www.cropmonitor.org/s/Special_Report_20230824_El_Nino.pdf",
        "GEOGLAM Crop Monitor", "Special Report: El Nino Aug 2023", "2023-08", "forecast",
        CONSENSUS, A23, BOTH, ["maize"]),
    doc("https://www.icpac.net/documents/755/GHACOF65_Technical_Statement.pdf",
        "ICPAC GHACOF", "GHACOF-65 statement (OND 2023, as issued)", "2023-08", "forecast",
        CONSENSUS, A23, KE),
    doc("https://www.sadc.int/sites/default/files/2023-09/FINAL%20SARCOF-27%20STATEMENT.pdf",
        "SADC SARCOF", "SARCOF-27 statement (2023/24 season, as issued)", "2023-09",
        "forecast", CONSENSUS, A23, ZM),
    doc("https://www.cropmonitor.org/s/Global_CropMonitor_202312.pdf",
        "GEOGLAM Crop Monitor", "Global Crop Monitor Dec 2023", "2023-12", "retrospective",
        CONSENSUS, A23, BOTH, ["maize"]),
    doc("https://www.fao.org/3/cc5749en/cc5749en.pdf",
        "FAO GIEWS", "El Nino to return: anticipated impacts (Apr 2023)", "2023-04",
        "forecast", AGENCY, A23),
    doc("https://www.fao.org/3/cd0509en/cd0509en.pdf",
        "FAO GIEWS", "Special Alert 352: Southern Africa drought (Apr 2024)", "2024-04",
        "retrospective", AGENCY, A23, ZM, ["maize"]),
    doc("https://ndma.go.ke/wp-content/uploads/2023/09/2023-Long-Rains-Assessment-Report_Full-version.pdf",
        "Kenya KFSSG", "2023 Long Rains Assessment", "2023-07", "retrospective", CONSENSUS,
        A23, KE, ["maize"]),
    doc("https://knowledgeweb.ndma.go.ke/Content/LibraryDocuments/National_Short_Rains_Assessment_Report20230510122036.pdf",
        "Kenya KFSSG", "2022 Short Rains Assessment (pre-El Nino baseline)", "2023-02",
        "retrospective", CONSENSUS, countries=KE, crops=["maize"],
        doc_type="pre-event-baseline"),
    doc("https://reliefweb.int/attachments/0be0d598-5c7e-4687-9c56-a11ff72593c2/IPC_Zambia_Acute_Food_Insecurity_Jul2024_Mar2025_Report.pdf",
        "IPC", "Zambia acute food insecurity Jul 2024 - Mar 2025", "2024-07",
        "retrospective", CONSENSUS, A23, ZM, ["maize"]),
    doc("https://reliefweb.int/attachments/2f365a35-3a35-4b6d-bb84-160b5ecc3271/Drought%20Response%20Situation%20Report%201%2019.04.2024.pdf",
        "Zambia DMMU", "Drought response situation report No.1 (Apr 2024)", "2024-04",
        "retrospective", AGENCY, A23, ZM, ["maize"]),
    doc("https://zambia.un.org/sites/default/files/2024-06/United%20Nations%20Zambia%20Information%20Update%20on%20Drought%20May%202024.pdf",
        "UN Zambia", "Information update on drought May 2024", "2024-05", "retrospective",
        AGENCY, A23, ZM),
    doc("https://www.acaps.org/fileadmin/Data_Product/Main_media/20250116_ACAPS_Zambia_-_Update_on_the_impact_of_drought_.pdf",
        "ACAPS", "Zambia drought impact update (Jan 2025)", "2025-01", "retrospective",
        AGENCY, A23, ZM),
    doc("https://wmo.int/sites/default/files/2025-05/Africa_2024final1.pdf",
        "WMO", "State of the Climate in Africa 2024", "2025-05", "retrospective",
        CONSENSUS, A23, BOTH),
    # ---- 2015-16 analog -----------------------------------------------------
    doc("https://www.cropmonitor.org/s/EarlyWarning_CropMonitor_201602.pdf",
        "GEOGLAM Crop Monitor", "Crop Monitor for Early Warning Feb 2016 (first issue)",
        "2016-02", "retrospective", CONSENSUS, A15, BOTH, ["maize"]),
    doc("https://www.fao.org/3/I5197E/I5197E.pdf",
        "FAO GIEWS", "Crop Prospects and Food Situation No.4 (Dec 2015)", "2015-12",
        "retrospective", AGENCY, A15, BOTH, ["maize"]),
    doc("https://www.fao.org/3/a-i5258e.pdf",
        "FAO GIEWS", "Special Alert 336: El Nino in Southern Africa (2015)", "2015-12",
        "forecast", AGENCY, A15, ZM, ["maize"]),
    doc("https://www.fao.org/3/i6049e/i6049e.pdf",
        "FAO", "El Nino response update #10 (2016)", "2016-08", "retrospective", AGENCY,
        A15, BOTH),
    doc("https://fews.net/sites/default/files/documents/reports/Kenya_OL_2015_10.pdf",
        "FEWS NET", "Kenya food security outlook Oct 2015", "2015-10", "forecast", AGENCY,
        A15, KE, ["maize"]),
    doc("https://reliefweb.int/sites/reliefweb.int/files/resources/report_on_the_riasco_action_plan_2016-2017_july2017.pdf",
        "RIASCO", "Report on the RIASCO Action Plan 2016-17 (Southern Africa El Nino)",
        "2017-07", "retrospective", CONSENSUS, A15, ZM, ["maize"]),
    doc("https://www.sadc.int/sites/default/files/2021-08/SARCOF_20_Statement.pdf",
        "SADC SARCOF", "SARCOF-20 statement (2016/17 season, post-El Nino recovery outlook)",
        "2016-08", "forecast", CONSENSUS, A15, ZM, doc_type="post-event-outlook"),
    # ---- bottom-up county exhibits ------------------------------------------
    doc("https://knowledgeweb.ndma.go.ke/Content/LibraryDocuments/KITUI_LRA_July_202520250825143809.pdf",
        "Kenya NDMA", "Kitui county long-rains assessment Jul 2025", "2025-07",
        "retrospective", AGENCY, countries=KE, crops=["maize"], doc_type="county"),
    doc("https://knowledgeweb.ndma.go.ke/Content/LibraryDocuments/Narok_DEW_Bulletin_April_202520250516124829.pdf",
        "Kenya NDMA", "Narok county drought early-warning bulletin Apr 2025", "2025-04",
        "retrospective", AGENCY, countries=KE, doc_type="county"),
    doc("https://knowledgeweb.ndma.go.ke/Content/LibraryDocuments/National_Long_Rains_Assessment_Report_202520250819170940.pdf",
        "Kenya KFSSG", "2025 Long Rains Assessment (pre-El Nino baseline)", "2025-08",
        "retrospective", CONSENSUS, countries=KE, crops=["maize"],
        doc_type="pre-event-baseline"),
]


# Hosts with documented-broken TLS chains (corpus-blueprint.md) — the only ones
# allowed the verify=False fallback, loudly.
_BROKEN_TLS_HOSTS = ("zamstats.gov.zm", "agriculture.gov.zm", "kilimo.go.ke")


def fetch(url: str) -> bytes:
    ua = None if "reliefweb.int" in url else BROWSER_UA  # reliefweb blocks fake UAs
    headers = {"User-Agent": ua} if ua else {}
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=(10, 90))
            if r.status_code >= 500:               # transient upstream -> retry
                last = requests.HTTPError(f"HTTP {r.status_code}")
            else:
                r.raise_for_status()
                return r.content
        except requests.exceptions.SSLError as exc:
            if not any(h in url for h in _BROKEN_TLS_HOSTS):
                raise                              # unexpected TLS failure: refuse, like the route
            print(f"  WARN unverified TLS (known-broken chain): {url}")
            r = requests.get(url, headers=headers, timeout=(10, 90), verify=False)
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:   # timeouts, resets, truncated bodies
            last = exc
        time.sleep(2 * attempt + 1)
    raise requests.RequestException(f"gave up on {url}: {last}")


def main() -> int:
    corpus = Corpus("food-security")
    ok, skipped, failed = 0, 0, []
    for entry in SOURCES:
        url, meta = entry["url"], entry["metadata"]
        name = entry["filename"] or url.split("?")[0].rsplit("/", 1)[-1] or "doc.pdf"
        try:
            raw = fetch(url)
            text = docloader.extract_text(raw, name)
            res = corpus.ingest(text, meta, raw=raw, filename=name)
            ok += 0 if res["already_ingested"] else 1
            skipped += 1 if res["already_ingested"] else 0
            print(f"  ok   {meta['source']}: {meta['title']}"
                  f" ({res['chunks']} chunks{', dup' if res['already_ingested'] else ''})")
        except Exception as exc:  # log and continue — a dead URL must not kill the seed
            failed.append((meta["title"], f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL {meta['source']}: {meta['title']} — {type(exc).__name__}: {str(exc)[:90]}")
    print(f"\ningested {ok} new, {skipped} duplicates, {len(failed)} failed / {len(SOURCES)} sources")
    print(f"corpus now: {len(corpus.documents())} documents, {len(corpus._chunks)} chunks")
    for title, err in failed:
        print(f"  failed: {title} — {err[:110]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
