"""Fast connect on a live-sized registry (500+ raster layers, 70 vector layers, 60 feeds, 16
hazards, 15+ countries): every connect-time text fits with its routing words, a rebuild reads
each source once in under a second, and a client is served from memory while one runs."""
import asyncio
import collections
import json
import logging
import re
import threading
import time

import pytest
import yaml

from app.config import get_settings

# --------------------------------------------------------------------------- the registry
COUNTRIES = ["Thailand", "Cambodia", "Viet Nam", "Indonesia", "Philippines", "Myanmar", "Nepal",
             "Bhutan", "Costa Rica", "Panama", "Ecuador", "Brazil", "Peru", "Guatemala", "Kenya",
             "Zambia"]
# (stack id, title, countries, first year, last year, legend) — titles as hubs write them
STACKS = [
    ("landcover_hkh", "HKH land cover 2000-2022 (ICIMOD RLCMS, 30 m, 9 classes)",
     ["Afghanistan", "Pakistan", "India", "Nepal", "Bhutan", "Bangladesh", "Myanmar", "China"],
     2000, 2022, ["Water body", "Snow and glacier", "Forest", "Riverbed", "Built-up area",
                  "Cropland", "Bare soil", "Bare rock", "Grassland"]),
    ("landcover_mapbiomas_br", "MapBiomas Brazil 10 m land cover, Collection 4 (Sentinel-2)",
     ["Brazil"], 2017, 2025, ["Forest", "Mangrove", "Wetland", "Grassland", "Pasture", "Cropland",
                              "Forest plantation", "Built-up", "Water", "Aquaculture"]),
    ("landcover_mapbiomas_ec", "MapBiomas Ecuador land cover, Collection 2", ["Ecuador"], 1985,
     2022, ["Forest", "Mangrove", "Shrubland", "Grassland", "Cropland", "Built-up", "Water",
            "Aquaculture", "Salt flat"]),
    ("landcover_mapbiomas_pe", "MapBiomas Peru land cover, Collection 2", ["Peru"], 1985, 2022,
     ["Forest", "Mangrove", "Shrubland", "Grassland", "Cropland", "Built-up", "Water"]),
    ("landcover_esa_wc_cr", "ESA WorldCover 10 m (2020 v100, 2021 v200), Costa Rica",
     ["Costa Rica"], 2020, 2021, ["Tree cover", "Shrubland", "Grassland", "Cropland", "Built-up",
                                  "Bare / sparse vegetation", "Snow and ice", "Water",
                                  "Herbaceous wetland", "Mangroves", "Moss and lichen"]),
    ("landcover_gwl_cr", "GWL_FCS30D global 30 m wetland maps (fine classification), Gulf of Nicoya",
     ["Costa Rica"], 2000, 2022, ["Mangrove", "Salt marsh", "Tidal flat", "Swamp", "Marsh",
                                  "Flooded flat", "Lake", "River", "Not wetland"]),
    ("landcover_gmw_pa", "Global Mangrove Watch v3 mangrove extent, Gulf of Chiriqui", ["Panama"],
     1996, 2020, ["Not mangrove", "Mangrove"]),
    ("landcover_gmw_id", "Global Mangrove Watch v3 mangrove extent, Demak", ["Indonesia"], 1996,
     2020, ["Not mangrove", "Mangrove"]),
    ("landcover_maga_gt", "MAGA Guatemala land use / land cover 2010-2025 (rasterised 30 m, grouped)",
     ["Guatemala"], 2010, 2025, ["Forest", "Mangrove", "Cropland", "Pasture", "Built-up", "Water",
                                 "Shrubland", "Wetland", "Bare soil", "Forest plantation",
                                 "Grassland", "Salt flat", "Aquaculture"]),
    ("landcover_dw_th", "Dynamic World land cover, Thailand (10 m)", ["Thailand"], 2016, 2024,
     ["Water", "Trees", "Grass", "Flooded vegetation", "Crops", "Shrub and scrub", "Built",
      "Bare", "Snow and ice"]),
    ("landcover_mekong_vn", "Mekong delta mangrove and aquaculture map, Viet Nam (SERVIR-Mekong)",
     ["Viet Nam"], 2000, 2023, ["Mangrove", "Aquaculture", "Rice", "Built-up", "Water", "Other"]),
    ("landcover_namria_ph", "NAMRIA Philippines land cover 2020 (national, 10 m)", ["Philippines"],
     2010, 2020, ["Forest", "Mangrove", "Brushland", "Cropland", "Grassland", "Built-up", "Water",
                  "Fishpond"]),
]
# The land-cover stacks as served on 2026-10-24 (platform_capabilities(section='coverage')):
# their real titles, countries and spans; one carries no title.
_WC = ["Tree cover", "Shrubland", "Grassland", "Cropland", "Built-up", "Bare / sparse vegetation",
       "Snow and ice", "Permanent water bodies", "Herbaceous wetland", "Mangroves", "Moss and lichen"]
_GMW = ["Not mangrove", "Mangrove"]
_GWL = ["Permanent water", "Swamp", "Marsh", "Flooded flat", "Saline", "Mangrove forest", "Salt marsh",
        "Tidal flat", "Not wetland"]
_MB = ["Forest formation", "Mangrove", "Wetland", "Grassland", "Pasture", "Cropland", "Forest plantation",
       "Built-up", "Water", "Aquaculture", "Salt flat", "Beach and dune", "Shrubland", "Rocky outcrop"]
LIVE_STACKS = [STACKS[0]] + [
    ("landcover_cr_nicoya_catie", "CATIE Gulf of Nicoya mangrove baseline 1956-1985-2014 (30 m, mangrove / non-mangrove)", ["Costa Rica"], 1956, 2014, ["Mangrove", "Non-mangrove"]),
    ("landcover_cr_gmw_v3_2class", "Global Mangrove Watch v3.0, mangrove / not mangrove, Costa Rica mosaic (1996-2020, 25 m)", ["Costa Rica"], 1996, 2020, _GMW),
    ("landcover_cr_worldcover", "ESA WorldCover 10 m (2020 v100, 2021 v200), Costa Rica", ["Costa Rica"], 2020, 2021, _WC),
    ("landcover_cr_nicoya_catiemask56", "CATIE Gulf of Nicoya mangrove mask 1956 (mangrove_56.img as delivered, mangrove only)", ["Costa Rica"], 1956, 1956, ["Mangrove"]),
    ("landcover_cr_nicoya_catiemask", "CATIE Gulf of Nicoya mangrove masks 1985 and 2014 (mangrove only, as delivered)", ["Costa Rica"], 1985, 2014, ["Mangrove"]),
    ("landcover_cr_gwl_fcs30d", "GWL_FCS30D global 30 m wetland maps (fine classification), Gulf of Nicoya", ["Costa Rica"], 2020, 2022, _GWL),
    ("landcover_cr_gwl_fcs30d_terraba", "GWL_FCS30D global 30 m wetland map, Térraba-Sierpe, Costa Rica, 2020-2022", ["Costa Rica"], 2020, 2022, _GWL),
    ("landcover_ec_mapbiomas", "MapBiomas Ecuador Collection 4.0 annual land cover and land use (30 m, 1985-2025)", ["Ecuador"], 1990, 2025, _MB),
    ("landcover_ec_mbwaterbg", "MapBiomas Agua Ecuador Collection 4.0 annual water surface, background as a class (30 m, 1985-2025)", ["Ecuador"], 1990, 2025, ["Water", "Background"]),
    ("landcover_ec_gmw_v3_2class_guayaquil", "Global Mangrove Watch v3.0 mangrove extent, Gulf of Guayaquil, Ecuador, 1996-2020 (25 m)", ["Ecuador"], 1996, 2020, _GMW),
    ("landcover_ec_gwl_fcs30d_guayaquil", "GWL_FCS30D global 30 m wetland map, Gulf of Guayaquil, Ecuador, 2020-2022", ["Ecuador"], 2020, 2022, _GWL),
    ("landcover_ec_worldcover_guayaquil", "ESA WorldCover 10 m, Gulf of Guayaquil, Ecuador", ["Ecuador"], 2021, 2021, _WC),
    ("landcover_ec_mapbiomas_cutoff", "MapBiomas Ecuador Collection 4.0, Gulf of Guayaquil, before and after the end of 2020 (2020 and 2025 only)", ["Ecuador"], 2020, 2025, _MB),
    ("landcover_ec_maate_cutoff", "MAATE official land cover (level-2 classes), Gulf of Guayaquil, before and after the end of 2020 (2020 and 2022 only), rasterised 30 m", ["Ecuador"], 2020, 2022, _MB[:10]),
    ("landcover_ec_mag_fertilidad_quimica_guayaquil", "MAG soil chemical fertility class (Fertilidad química de los suelos), Gulf of Guayaquil, 30 m, 2022 — a soil map, not land cover", ["Ecuador"], 2022, 2022, ["Very low", "Low", "Medium", "High", "Very high"]),
    ("landcover_ec_mag_fertilidad_quimica_esmeraldas", "MAG soil chemical fertility class (Fertilidad química de los suelos), Esmeraldas mangroves, 30 m, 2022 — a soil map, not land cover", ["Ecuador"], 2022, 2022, ["Very low", "Low", "Medium", "High", "Very high"]),
    ("landcover_br_mb10m", "MapBiomas Brazil 10 m land cover, Collection 4 (Sentinel-2)", ["Brazil"], 2017, 2025, _MB),
    ("landcover_br_gwl_fcs30d_br_para", "GWL_FCS30D global 30 m wetland map, Pará coast (Bragança–Salinópolis), Brazil, 2020-2022", ["Brazil"], 2020, 2022, _GWL),
    ("landcover_br_worldcover_br_para", "ESA WorldCover 10 m, Pará coast (Bragança–Salinópolis), Brazil", ["Brazil"], 2021, 2021, _WC),
    ("landcover_br_mb10m_salinopolis", "MapBiomas Brazil Collection 4 (10 m), Salinópolis, 2017-2025", ["Brazil"], 2017, 2025, _MB),
    ("landcover_br_gmw_v3_2class_br_para", None, ["Brazil"], 1996, 2020, _GMW),
    ("landcover_gt_maga_pacificext", "MAGA Guatemala land use / land cover 2003-2025 (1:50,000, rasterised 30 m by the platform, grouped), Pacific coast to 14.6 N", ["Guatemala"], 2003, 2025, _MB[:13]),
    ("landcover_gt_gmw_v3_2class_gt_pacific", "Global Mangrove Watch v3.0 mangrove extent, Pacific coast, Guatemala, 1996-2020 (25 m)", ["Guatemala"], 1996, 2020, _GMW),
    ("landcover_gt_gwl_fcs30d_gt_pacific", "GWL_FCS30D global 30 m wetland map, Pacific coast, Guatemala, 2020-2022", ["Guatemala"], 2020, 2022, _GWL),
    ("landcover_gt_worldcover_gt_pacific", "ESA WorldCover 10 m, Pacific coast, Guatemala", ["Guatemala"], 2021, 2021, _WC),
    ("landcover_ke_gmw_v3_2class_ke_coast", "Global Mangrove Watch v3.0 mangrove extent, Kenya coast (Vanga to Lamu), 1996-2020 (25 m)", ["Kenya"], 1996, 2020, _GMW),
    ("landcover_ke_gwl_fcs30d_ke_coast", "GWL_FCS30D global 30 m wetland map, Kenya coast (Vanga to Lamu), 2020-2022", ["Kenya"], 2020, 2022, _GWL),
    ("landcover_ke_worldcover_lamu", "ESA WorldCover 10 m, Lamu, Kenya", ["Kenya"], 2021, 2021, _WC),
    ("landcover_id_gmw_v3_2class_demak", "Global Mangrove Watch v3.0 mangrove extent, Demak coast, Central Java, Indonesia, 1996-2020 (25 m)", ["Indonesia"], 1996, 2020, _GMW),
    ("landcover_id_mangrove_extent", "Mangrove extent, Indonesia", ["Indonesia"], 2017, 2024, _GMW),
    ("landcover_id_ssumatra_alos", "South Sumatra geomorphology: ALOS landform classes", ["Indonesia"], 2026, 2026, [f"Landform {i}" for i in range(15)]),
    ("landcover_th_gmw_phuket", "Global Mangrove Watch v3.0 mangrove extent, Phuket Province, Thailand, 1996-2020 (25 m)", ["Thailand"], 1996, 2020, _GMW),
    ("landcover_th_worldcover_phuket", "ESA WorldCover 10 m, Phuket Province, Thailand", ["Thailand"], 2021, 2021, _WC),
    ("landcover_vn_gmw_camau", "Global Mangrove Watch v3.0 mangrove extent, Cà Mau coast, Vietnam, 1996-2020 (25 m)", ["Vietnam"], 1996, 2020, _GMW),
    ("landcover_vn_wdpa_camau", "WDPA protected areas (rasterised), Cà Mau coast, Vietnam", ["Vietnam"], 2026, 2026, ["Protected", "Not protected", "Proposed"]),
    ("landcover_sn_gmw_saloum", "Global Mangrove Watch v3.0 mangrove extent, Senegal coast (Saloum delta and lower Casamance), 1996-2020 (25 m)", ["Senegal", "The Gambia"], 1996, 2020, _GMW),
]
# Extension packs with keywords as long as a real pack's.
EXTRA_PACKS = [
    ("water", "water security: river discharge, reservoir storage, groundwater drought and flood early "
              "warning (Mekong, Ganges-Brahmaputra and Zambezi basins)", ("basin", "date")),
    ("air", "air quality: PM2.5 and ozone forecasts, smoke from agricultural burning and haze episodes "
            "(Thailand, Lao PDR, Myanmar, Viet Nam)", ("place", "date")),
    ("health", "climate and health: dengue and malaria risk, heat stress days and vector suitability "
               "(Southeast Asia and East Africa)", ("place", "month")),
]
HAZARDS = ["flood", "flashflood", "drought", "fire", "landslides", "cyclone", "storm", "tsunami",
           "earthquake"]
RETURN_PERIODS = [10, 20, 50, 100, 200, 500]
SEA_BOX = [92.0, -11.1, 141.2, 28.7]
# Publishers as contributors write them: full citations, consortia with their partners.
PUBLISHERS = [
    "First-Author, A. and Second-Author, B. (2023). Dataset: Study of total economic valuation "
    "of the main services provided by mangroves in the Gulf of Chiriqui, Panama. Coastal Carbon "
    "Network, a research centre's data portal. doi:10.0000/example.24294928",
    "ADPC GRP Bangkok flood pilot (derived from Floodboard: BMA, Traffy Fondue and public reports; "
    "facilities from OpenStreetMap)",
    "ADPC GRP Bangkok flood pilot, derived from Floodboard public sources and OpenStreetMap facilities",
    "Bio-ORACLE consortium (Flanders Marine Institute VLIZ and partners)",
    "Bio-ORACLE v3.0 ERDDAP; Bio-ORACLE v3 paper, Global Ecology and Biogeography 2024, DOI "
    "10.1111/geb.13813",
    "GBIF (Global Biodiversity Information Facility), aggregating its publishing institutions",
    "OBIS (Ocean Biodiversity Information System, IOC-UNESCO), aggregating its publishing datasets",
    "The Nature Conservancy, Mapping Ocean Wealth (maps.oceanwealth.org), cumulus-ags.tnc.org "
    "nascience/Mapping_Ocean_Wealth MapServer layer 224; model by TNC with IUCN on the Global "
    "Mangrove Watch v3 mangrove typology",
    "Third-Author, C., Fourth-Author, D. et al. (2020). Total ecosystem carbon stocks of mangroves "
    "across broad global environmental and physical gradients. A journal 90(2). "
    "doi:10.0000/example.1405",
    "Instituto Nacional de Estadistica y Censos (INEC), Encuesta de Superficie y Produccion "
    "Agropecuaria Continua 2023, Quito",
]


def _legend(classes):
    return {i + 1: c for i, c in enumerate(classes)}


def live_sized(root, stacks=STACKS):
    """Write a live-sized catalog and schema under `root` with `stacks`; return them with 70
    vector layers and 60 feeds: {"settings": {field: path}, "vectors": {...}, "feeds": {...}}."""
    from pathlib import Path
    root = Path(root)
    conf = root / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    cat, contracts = {}, {}

    def row(key, title, ns, countries=(), bbox=None, **extra):
        r = {"title": title, "description": f"{title}: a live-sized registry row",
             "source": PUBLISHERS[len(cat) % len(PUBLISHERS)], "license": "CC-BY-4.0",
             "vintage": "2024", "local_path": f"{key}.tif", "role": ns, "contributed": True,
             "countries": list(countries)}
        if bbox:
            r["bbox"] = bbox
        r.update(extra)
        cat[key] = r
        contracts[key] = {"dtype": "float32", "valid_min": 0, "valid_max": 1000,
                          "units": "per area", "pixel_size_deg": 0.000277}

    for h in HAZARDS:
        row(f"hazard_{h}", f"{h.capitalize()} hazard (Southeast Asia)", "hazard", bbox=SEA_BOX,
            legend={1: "very low", 2: "low", 3: "medium", 4: "high", 5: "very high"})
    for rp in RETURN_PERIODS:
        row(f"hazard_flood_rp{rp}", f"{rp}-year flood hazard (Southeast Asia, JRC)", "hazard",
            bbox=SEA_BOX)
    row("hazard_cr_flood", "Flood depth (Fathom, 90 m)", "hazard", ["Costa Rica"],
        bbox=[-86.0, 8.0, -82.5, 11.3])
    row("hazard_heatdays", "Extreme heat-day hazard (demonstration)", "hazard",
        description="SYNTHETIC DEMONSTRATION LAYER — not a scientific product")
    for sid, title, countries, y0, y1, classes in stacks:
        years = list(range(y0, y1 + 1))
        cat[sid] = {"kind": "stack", "role": "landcover", "internal": True, "title": title,
                    "description": f"{title}: a live-sized stack", "countries": countries,
                    "source": PUBLISHERS[len(cat) % len(PUBLISHERS)], "license": "CC-BY-4.0",
                    "pub_date": "2024-01-01", "accuracy": "unstated", "legend": _legend(classes),
                    "outside_values": [0], "years": years, "layer_pattern": sid + "_{year}",
                    "file_pattern": sid + "_{year}.tif", "data_root": str(root / "stacks"),
                    "extent": {"lon": [-180, 180], "lat": [-60, 60]}}
        if not title:
            cat[sid].pop("title")
    for i in range(30):
        row(f"measure_biooracle_{i}", f"Bio-ORACLE sea surface variable {i} mean 2000-2018 (v3.0)",
            "measure", bbox=[-180, -60, 180, 60])
    for c in COUNTRIES:
        slug = c.lower().replace(" ", "_")
        row(f"measure_agb_{slug}", f"Mangrove above-ground biomass, {c} (GMW, 2020)", "measure", [c])
        row(f"measure_soc_{slug}", f"Soil organic carbon stock, {c} (SoilGrids 250 m)", "measure", [c])
        row(f"measure_canopy_{slug}", f"Canopy height, {c} (ETH GEDI 2020, 10 m)", "measure", [c])
        row(f"population_{slug}_2026", f"Population count, {c} 2026 (WorldPop R2025A, 100 m)",
            "population", [c])
        row(f"change_gmw_{slug}", f"Global Mangrove Watch mangrove change 1996-2020, {c}",
            "change", [c], legend={1: "loss", 2: "gain", 3: "stable"})
        for j in range(4):
            row(f"vulnerability_{slug}_{j}", f"Vulnerability index {j}, {c} (hub survey 2023)",
                "vulnerability", [c])
    n = 0
    while len(cat) - len(stacks) < 520:
        c = COUNTRIES[n % len(COUNTRIES)]
        row(f"measure_hub_{n}", f"Hub field plot carbon survey {n}, {c} (national inventory)",
            "measure", [c])
        n += 1
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    shipped = json.loads(json.dumps(__import__("yaml").safe_load(
        open(get_settings().raster_schema_path)) or {}))
    (conf / "raster_schema.yml").write_text(json.dumps(
        {"defaults": shipped.get("defaults") or {"crs": "EPSG:4326"},
         "namespaces": shipped.get("namespaces") or {}, "layers": contracts,
         "stacks": {sid: {"role": "landcover", "dtype": "uint8", "valid_min": 0, "valid_max": 20,
                          "nodata": 0, "pixel_size_deg": 0.000277} for sid, *_ in stacks}}))
    kinds = [("points", "Evacuation centres and shelters ({c}, DDPM)"),
             ("lines", "Rivers and drainage channels ({c}, HydroRIVERS)"),
             ("polygons", "Protected areas ({c}, WDPA)"),
             ("points", "Early-warning towers ({c}, hub upload)"),
             ("polygons", "Official mangrove map 2021 ({c}, national agency)")]
    vectors = {}
    for i in range(70):
        fam, title = kinds[i % len(kinds)]
        c = COUNTRIES[i % len(COUNTRIES)]
        inside = c in ("Thailand", "Cambodia", "Viet Nam", "Indonesia", "Philippines", "Myanmar")
        vectors[f"hub_layer_{i:02d}"] = {
            "title": title.format(c=c) + f" set {i}", "geometry": fam, "countries": [c],
            "bbox": [100.2, 13.5, 100.8, 14.0] if inside else [-85.3, 9.9, -84.7, 10.3],
            "local_path": f"vectors/hub_layer_{i:02d}.geojson"}
    feeds = {}
    for i in range(60):
        c = COUNTRIES[i % len(COUNTRIES)]
        feeds[f"hub_feed_{i:02d}"] = {
            "status": "available", "pack": ("food-security", "land-cover", "risk")[i % 3],
            "adapter": "generic_json", "title": f"Hub {i} monthly survey table ({c}, live API)",
            "source": PUBLISHERS[i % len(PUBLISHERS)] + (f" — table {i}" if i >= len(PUBLISHERS) else ""),
            "countries": [c], "description": "a live-sized feed row", "contributed": True}
    return {"settings": {"tiffs_config_path": conf / "tiffs.yml",
                         "tiffs_contrib_path": conf / "tiffs.contrib.yml",
                         "raster_schema_path": conf / "raster_schema.yml",
                         "raster_schema_contrib_path": conf / "raster_schema.contrib.yml"},
            "vectors": vectors, "feeds": feeds}


def install(reg, setattr_, setitem):
    """Point the platform at a live_sized() registry (setattr_/setitem: monkeypatch's)."""
    from app.graph.geo import vectors
    from app.mcp import registry
    s = get_settings()
    for k, v in reg["settings"].items():
        setattr_(s, k, v)
    setattr_(vectors, "landed", lambda: dict(reg["vectors"]))
    for k, v in reg["feeds"].items():
        setitem(registry.FEEDS, k, v)


@pytest.fixture
def live(tmp_path, monkeypatch):
    reg = live_sized(tmp_path)
    install(reg, monkeypatch.setattr, monkeypatch.setitem)
    return reg


@pytest.fixture
def live_like(tmp_path, monkeypatch):
    """The live-sized registry with the land-cover stacks live serves."""
    reg = live_sized(tmp_path, LIVE_STACKS)
    install(reg, monkeypatch.setattr, monkeypatch.setitem)
    return reg


# --------------------------------------------------------------------------- the client's view
def _connect() -> tuple[str, dict, float]:
    """(instructions, {tool: description}, ms) as a client connecting now is served: the
    initialize handshake and tools/list, timed until the list arrives."""
    from fastmcp import Client
    from app.mcp.server import mcp

    async def go():
        t0 = time.perf_counter()
        async with Client(mcp, mode="legacy") as c:
            tools = await c.list_tools()
            ms = (time.perf_counter() - t0) * 1000
            return c.initialize_result.instructions, {t.name: t.description or "" for t in tools}, ms
    return asyncio.run(go())


def _routing(desc: str) -> str:
    return desc.split("\n\n")[0]


# --------------------------------------------------------------------------- 1-2. the texts
def test_every_text_fits_a_live_sized_registry_with_no_overrun(live, caplog, log):
    """Each generated description inside budget.TOOL_DESCRIPTION and the instructions inside
    both of theirs, built the way a refresh builds them and served through the handshake."""
    from app.mcp import budget, instructions, server
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        instr, descs = server.LIVE.build()
        core = instructions.core()
    sizes = {n: len(d) for n, d in descs.items()}
    log("OUTPUT", f"{sizes}; instructions {len(instr):,}, core {len(core):,}")
    assert set(descs) == set(server.DESCRIBERS)
    assert all(n <= budget.TOOL_DESCRIPTION for n in sizes.values()), sizes
    assert len(instr) <= budget.INSTRUCTIONS and len(core) <= budget.INSTRUCTIONS_CORE
    assert not caplog.messages, caplog.messages
    said, tools, _ms = _connect()
    assert said == instr and all(tools[n] == d for n, d in descs.items())


def test_the_routing_paragraph_names_the_countries_products_and_hazards(live, log):
    """Countries, product families and hazards keep their names; feeds and publishers go to a
    count with where each is named, and no citation-style publisher is ever said whole."""
    from app.mcp import server
    _instr, descs = server.LIVE.build()
    first = _routing(descs["assemble_pack"])
    log("OUTPUT", f"{len(first):,} chars: {first}")
    every = set(COUNTRIES) | {c for _sid, _t, cs, *_ in STACKS for c in cs}
    assert not [c for c in sorted(every) if c not in first]
    for family in ("MapBiomas", "Global Mangrove Watch", "ESA WorldCover", "GWL_FCS30D"):
        assert family in first, family
    for h in HAZARDS:
        assert h in first, h
    assert "flood at 10- to 500-year return periods" in first
    assert "Live feeds: " in first and "(feeds_query)" in first
    assert "publishers (platform_capabilities(section='coverage'))" in first
    assert not any(p in d for p in PUBLISHERS for d in descs.values())


def test_a_publisher_is_said_by_its_organisation_or_first_clause(log):
    """Short names for the citation-style publishers hubs write, each at most 48 chars; two
    wordings of one pilot say it once."""
    from app.mcp import registry
    short = [registry.short_name(p) for p in PUBLISHERS]
    log("OUTPUT", short)
    assert short[:5] == ["First-Author", "ADPC GRP Bangkok flood pilot", "ADPC GRP Bangkok flood pilot",
                         "Bio-ORACLE consortium", "Bio-ORACLE v3.0 ERDDAP"]
    assert all(0 < len(s) <= 49 for s in short)
    assert registry.short_name("https://data.example.org/a/b") == "data.example.org"
    assert registry.short_name("U.S. Geological Survey") == "U.S. Geological Survey"
    assert registry.short_name("Univ. Reading (Weather & Forecasting 35(5))") == "Univ. Reading"


def test_lists_go_to_a_count_by_rank_and_the_routing_words_last(log):
    """Publishers go first, then feed titles, then layer lists; routing names keep what room
    is left (the largest cap that fits, bisected)."""
    from app.mcp import budget
    route, layer, feed, pub = ([f"{w}{i}" for i in range(12)] for w in ("country", "layer", "feed", "pub"))

    def render():
        return " | ".join((budget.names(route, rank=budget.ROUTE), budget.names(layer),
                           budget.names(feed, rank=budget.FEED, counted="{n} feeds"),
                           budget.names(pub, rank=budget.SOURCE, counted="{n} publishers")))
    whole = render()
    text, ok = budget.fitted(render, lambda t: len(t) <= len(whole) - 150)
    log("OUTPUT", text)
    assert ok and "12 publishers" in text and "12 feeds" in text
    assert ", ".join(route) in text and "layer0" in text and "more" in text.split(" | ")[1]
    text, ok = budget.fitted(render, lambda t: len(t) <= 160)
    log("OUTPUT", text)
    assert ok and "country0" in text and "12 more" in text.split(" | ")[1]
    assert budget.names(["a", "b", "c", "d"], keep=2) == "a, b, c, d"
    with budget.capped(0):
        assert budget.names(["alpha", "beta", "gamma", "delta"], keep=2) == "alpha, beta and 2 more"


def test_a_list_is_never_counted_into_something_longer_or_ungrammatical(log):
    """No 'and 0 more' when every name fits once joined, no count longer than the names it
    stands for, the singular for one, and a routing list names its first two at any cap."""
    from app.mcp import budget
    said = []
    with budget.capped({budget.ROUTE: 14, budget.LIST: 46}):
        said.append(budget.names(["Kenya", "Zambia"], last=" and ", more="more countries",
                                 rank=budget.ROUTE))
        said.append(budget.names(["people", "roads", "schools", "hospitals", "buildings"],
                                 last=" and ", more="more asset layers", keep=5))
    with budget.capped(0):
        said.append(budget.names(["maize"], more="more crops"))
        said.append(budget.names(["Southeast Asia", "Taiwan", "Costa Rica"], sep="; ",
                                 more="more regions", rank=budget.ROUTE))
        said.append(budget.names(["Southeast Asia", "Taiwan", "the Pacific coast of Guatemala"],
                                 sep="; ", more="more regions", rank=budget.ROUTE))
        said.append(budget.names(["hazard number one", "hazard number two"], more="more hazards"))
        said.append(budget.names(["A Long Publishing Organisation"], counted="{n} publishers (x)"))
        said.append(budget.names(["FAO"], counted="{n} publishers (platform_capabilities)"))
    log("OUTPUT", said)
    assert said == ["Kenya and Zambia", "people, roads, schools, hospitals and buildings", "maize",
                    "Southeast Asia; Taiwan; Costa Rica", "Southeast Asia; Taiwan and 1 more region",
                    "2 more hazards", "1 publisher (x)", "FAO"]


# --------------------------------------------------------------------------- 3. one look
def test_a_rebuild_reads_each_source_once_even_when_it_refits(live, monkeypatch, log):
    """Every source registered with budget.once — the catalog, coverage, raster layers, vector
    layers, the schema, methods, the library — is computed once per rebuild though the
    assemble_pack description is rendered again and again to fit."""
    from app.contrib import methods
    from app.graph.geo import schema, tiffs, vectors
    from app.mcp import budget, packs, registry, server
    for fn in (tiffs.catalog, registry.coverage, registry.raster_layers, registry._corpus_summary,
               vectors.visible, schema.namespaces, methods.for_pack, packs.risk_coverage):
        assert fn in budget.SOURCES, fn.__name__
    calls = collections.Counter()
    for fn in budget.SOURCES:
        def counted(*a, _inner=fn.__wrapped__, _name=f"{fn.__module__}.{fn.__qualname__}", **k):
            calls[(_name, a, tuple(sorted(k.items())))] += 1
            return _inner(*a, **k)
        monkeypatch.setattr(fn, "__wrapped__", counted)
    renders = collections.Counter()
    real = registry._assemble_text
    monkeypatch.setattr(registry, "_assemble_text", lambda: (renders.update(["assemble_pack"]), real())[1])
    server.LIVE.build()
    log("OUTPUT", f"{renders['assemble_pack']} renders; {len(calls)} sources read: "
                  + ", ".join(f"{k[0].split('.')[-1]}{k[1] or ''}x{n}" for k, n in calls.items()))
    assert renders["assemble_pack"] > 1, "the live-sized text must have needed a refit"
    assert calls and max(calls.values()) == 1, calls.most_common(3)
    assert any(k[0].endswith("tiffs.catalog") for k in calls) and any(k[0].endswith("coverage") for k in calls)


# --------------------------------------------------------------------------- 4-6. served from memory
def test_connect_is_served_from_memory_while_a_rebuild_is_slow(live, monkeypatch, log):
    """A stale cache is served at once (initialize and tools/list in < 100 ms) while a 2 s
    rebuild runs in the background; the next connection gets the rebuilt text."""
    from app.mcp import server
    _connect()                                            # the first build, in line
    before = _connect()[1]["ui_catalog"]

    def slow():
        time.sleep(2)
        return "ui_catalog, rebuilt slowly"
    monkeypatch.setattr(server, "DESCRIBERS", {"ui_catalog": slow, **{
        k: v for k, v in server.DESCRIBERS.items() if k != "ui_catalog"}})
    server.LIVE._at = time.monotonic() - server.LIVE.TTL - 1
    said, tools, ms = _connect()
    t0 = time.perf_counter()
    server.LIVE.wait(10)
    took = time.perf_counter() - t0
    log("OUTPUT", f"served stale in {ms:.1f} ms; the rebuild finished {took:.2f} s later")
    assert ms < 100 and tools["ui_catalog"] == before and said
    assert took > 1.0
    _said, tools, ms = _connect()
    assert tools["ui_catalog"] == "ui_catalog, rebuilt slowly" and ms < 100


def test_a_full_rebuild_takes_under_a_second(live, caplog, log):
    """Startup and every refresh pay one rebuild: on the live-sized registry, under a second,
    logged once with its duration."""
    from app.mcp import server
    with caplog.at_level(logging.INFO, logger="app.mcp.server"):
        t0 = time.perf_counter()
        server.LIVE.rebuild("test")
        took = time.perf_counter() - t0
    log("OUTPUT", f"{took * 1000:.0f} ms; {caplog.messages}")
    assert took < 1.0
    assert [m for m in caplog.messages if "connect-time text rebuilt in" in m and "(test)" in m]


def test_a_freshly_contributed_layer_and_feed_appear_after_a_refresh(live, monkeypatch, log):
    """A stack lands in the gate's catalog file and a feed is approved: the next connection is
    served at once, the one after the refresh names the stack's country (the file changed) and
    the feed (past the TTL); the stack's title is in the coverage section."""
    from app.mcp import registry, server
    first = _connect()[1]
    assert "Bahamas" not in first["assemble_pack"]
    real = server.LIVE.build                      # a refresh that lands after the request
    monkeypatch.setattr(server.LIVE, "build", lambda: (time.sleep(0.5), real())[1])
    contrib = get_settings().tiffs_contrib_path
    contrib.write_text(json.dumps({"landcover_bnt_bs": {
        "kind": "stack", "role": "landcover", "title": "Bahamas National Trust mangrove extent 2024",
        "countries": ["Bahamas"], "legend": {1: "Mangrove", 2: "Not mangrove"}, "years": [2024],
        "contributed": True, "source": "Bahamas National Trust", "license": "CC-BY-4.0"}}))
    _said, served, _ms = _connect()
    assert "Bahamas" not in served["assemble_pack"]                     # stale, refreshing
    server.LIVE.wait(10)
    now = _connect()[1]["assemble_pack"]
    log("OUTPUT", _routing(now))
    assert "Bahamas" in _routing(now)
    assert "Bahamas National Trust mangrove extent" in json.dumps(registry.coverage_by_name())
    monkeypatch.setitem(registry.FEEDS, "bnt_mangrove_survey", {
        "status": "available", "pack": "land-cover", "adapter": "generic_json",
        "title": "Bahamas mangrove survey (BNT, live API)", "source": "Bahamas National Trust"})
    title = "Bahamas mangrove survey (BNT, live API)"
    assert title not in _connect()[1]["feeds_query"]
    server.LIVE._at = time.monotonic() - server.LIVE.TTL - 1
    _connect()
    server.LIVE.wait(10)
    assert title in _connect()[1]["feeds_query"]


def test_a_pack_beyond_the_three_routes_through_the_same_seam(live, monkeypatch, log):
    """A fourth pack's own keywords hook reaches the routing paragraph, and a country it names
    is not said again among the other countries with data."""
    from app.mcp import packs, server
    monkeypatch.setitem(packs.PACKS, "reef", {
        "display_name": "Reef Platform", "target_keys": ("site",), "target_doc": {"site": "a reef"},
        "keywords": lambda: "coral reef health and bleaching (Fiji, Cambodia)",
        "gather": lambda *a, **k: ([], [], None), "sections": lambda *a: []})
    desc = server.LIVE.build()[1]["assemble_pack"]
    first = _routing(desc)
    log("OUTPUT", first)
    assert "coral reef health and bleaching (Fiji, Cambodia)" in first and "`reef` (site)" in desc
    assert first.count("Cambodia") == 1


def test_the_signature_follows_every_store_a_contribution_lands_in(live, tmp_path, monkeypatch, log):
    """A landed raster row, a declarative feed file, an approved vector layer and a document
    each change the signature that makes the served text stale before its TTL."""
    from app.graph.geo import vectors
    from app.mcp import packs, server
    from app.rag.store import corpus_dir
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds")
    corpus = next(sp["corpus"] for sp in packs.PACKS.values() if sp.get("corpus"))
    landings = [s.tiffs_contrib_path, tmp_path / "feeds" / "hub.yml",
                tmp_path / "cache" / vectors.REGISTRY, corpus_dir(corpus) / "meta.json"]
    for path in landings:
        path.parent.mkdir(parents=True, exist_ok=True)
    for path in landings:
        before = server._signature()
        time.sleep(0.01)
        path.write_text("{}")
        assert server._signature() != before, path
    log("CHECK", f"{len(landings)} stores, {len(server._signature())} paths watched")


# --------------------------------------------------------------------------- the live stacks
def test_the_live_stack_titles_route_by_product_family(live_like, caplog, log):
    """The stacks live serves: each product family named once — five MapBiomas titles say
    'MapBiomas' — the broadest first, with every stack's country; no product ends on a dangling
    word and an untitled stack shows no id."""
    from app.mcp import packs, server
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        _instr, descs = server.LIVE.build()
    first = _routing(descs["assemble_pack"])
    products, _countries = packs._lc_maps()
    log("OUTPUT", f"{len(descs['assemble_pack']):,} chars; products {products}; {first}")
    assert not caplog.messages, caplog.messages
    assert products[:5] == ["Global Mangrove Watch", "HKH land cover", "ESA WorldCover",
                            "GWL_FCS30D global wetland", "MapBiomas"]
    for family in ("Global Mangrove Watch", "HKH", "WorldCover", "GWL_FCS30D", "MapBiomas"):
        assert first.count(family) == 1, family
    assert not [c for c in {c for _s, _t, cs, *_ in LIVE_STACKS for c in cs} if c not in first]
    assert not [p for p in products if p.split()[-1].casefold() in packs._TRAILING]
    assert "landcover_br_gmw" not in json.dumps(descs)


@pytest.mark.parametrize("n", [2, 3])
def test_more_packs_keep_the_routing_names_and_the_grammar(live_like, monkeypatch, caplog, n, log):
    """Two or three more packs with keywords as long as a real pack's: each routing list still
    names its first two, never 'and 0 more' or '1 more <plural>'; two fit, three may overrun but
    say so in the log."""
    from app.mcp import budget, packs, server
    for pid, kw, keys in EXTRA_PACKS[:n]:
        monkeypatch.setitem(packs.PACKS, pid, {
            "display_name": pid.title() + " Platform", "target_keys": keys,
            "target_doc": {k: "x" for k in keys}, "keywords": (lambda kw=kw: kw),
            "gather": lambda *a, **k: ([], [], None), "sections": lambda *a: []})
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        _instr, descs = server.LIVE.build()
    d = descs["assemble_pack"]
    first = _routing(d)
    log("OUTPUT", f"{n} more packs: {len(d):,} chars, logged {caplog.messages}; {first}")
    for name, text in descs.items():
        assert not re.search(r"\b0 more\b|\b1 more \w+s\b", text), name
    for words in ("maize", "Kenya and Zambia", "Global Mangrove Watch, HKH land cover",
                  "Afghanistan, Pakistan", "flood, flashflood", "Southeast Asia", *[k for _p, k, _t in EXTRA_PACKS[:n]]):
        assert words in first, words
    if n == 2:
        assert len(d) <= budget.TOOL_DESCRIPTION and not caplog.messages
    else:
        assert len(d) <= budget.TOOL_DESCRIPTION or any("assemble_pack" in m for m in caplog.messages)


def test_feeds_query_names_each_feed_with_its_dataset_and_every_pending_one(live, log):
    """Seventy available feeds: the platform's own named first, each title with the `dataset` a
    call takes; every pending id survives (asking returns why); the rest is counted with where
    every one is named."""
    from app.mcp import budget, registry, server
    _instr, descs = server.LIVE.build()
    f = descs["feeds_query"]
    avail = {n: s for n, s in registry.FEEDS.items() if s.get("status") == "available"}
    own = [n for n, s in avail.items() if not s.get("contributed")]
    pending = [n for n, s in registry.FEEDS.items() if s.get("status") != "available"]
    named = [n for n in avail if f"`{n}`" in f]
    log("OUTPUT", f"{len(f):,} chars; {len(named)} of {len(avail)} available named, "
                  f"{len(pending)} pending: {f.split(chr(10))[0][:400]}")
    assert len(f) <= budget.TOOL_DESCRIPTION and own and pending
    assert not [n for n in own + pending if f"`{n}`" not in f]
    assert not [n for n in named if avail[n].get("title") and f"{avail[n]['title']} `{n}`" not in f]
    assert len(named) < len(avail) and "more (platform_capabilities(section='feeds'))" in f


# --------------------------------------------------------------------------- shared interpreter
def test_a_cpu_bound_rebuild_never_holds_up_a_connect(live, monkeypatch, log):
    """A rebuild that computes for 2 s shares the interpreter with the request: a connect taken
    while it runs is slower than an idle one, but never waits for it (< 500 ms each)."""
    from app.mcp import server
    _connect()

    def busy():
        end = time.perf_counter() + 2
        while time.perf_counter() < end:
            pass
        return "ui_catalog, rebuilt busily"
    monkeypatch.setattr(server, "DESCRIBERS", {"ui_catalog": busy, **{
        k: v for k, v in server.DESCRIBERS.items() if k != "ui_catalog"}})
    server.LIVE._at = time.monotonic() - server.LIVE.TTL - 1
    _connect()                                           # served stale; the rebuild starts
    time.sleep(0.05)
    took = [_connect()[2] for _ in range(3)]
    running = server.LIVE._thread is not None and server.LIVE._thread.is_alive()
    server.LIVE.wait(10)
    log("OUTPUT", f"connects during a CPU-bound rebuild: {[round(t) for t in took]} ms; "
                  f"rebuild still running after them: {running}")
    assert running and max(took) < 500
    assert _connect()[1]["ui_catalog"] == "ui_catalog, rebuilt busily"


# --------------------------------------------------------------------------- feeds, every worker
_FEED_SPEC = {"description": "a feed the gate landed", "source": "test fixture",
              "validation": "unvalidated", "residency": "external call-out", "cadence": "monthly",
              "adapter": "generic_json",
              "fetch": {"url": "https://example.invalid/rows.json", "records_path": "rows",
                        "fields": {"v": "v"}}}


@pytest.fixture
def feeds_dir(tmp_path, monkeypatch):
    from app.mcp import registry
    d = tmp_path / "feeds"
    d.mkdir()
    monkeypatch.setattr(get_settings(), "feeds_conf_dir", d)
    registry.reload_declarative_feeds()
    yield d
    monkeypatch.undo()
    registry.reload_declarative_feeds()


def _land(folder, name, title):
    """A feed file as the gate writes it (written whole, then moved into place)."""
    tmp = folder / f".{name}.tmp"
    tmp.write_text(yaml.safe_dump({"dataset": name, "title": title, **_FEED_SPEC}))
    tmp.replace(folder / f"{name}.yml")


def test_a_feed_reload_racing_a_rebuild_is_never_half_read(feeds_dir, monkeypatch, log):
    """An approval lands its file, then reloads the feeds (slowly); a connect mid-reload starts a
    rebuild. The text never drops a feed it named, and the reload's new generation makes the
    text stale until it names the new feed."""
    from app.contrib import feedspecs
    from app.mcp import registry, server
    for name in ("race_a", "race_b"):
        _land(feeds_dir, name, f"Race feed {name}")
    registry.reload_declarative_feeds()
    assert all(f"`{n}`" in _connect()[1]["feeds_query"] for n in ("race_a", "race_b"))
    before = server._signature()
    registry.reload_declarative_feeds()
    assert server._signature() != before                    # a reload is a change by itself
    real = feedspecs.load_dir
    monkeypatch.setattr(feedspecs, "load_dir", lambda d: (time.sleep(0.3), real(d))[1])
    _land(feeds_dir, "race_c", "Race feed race_c")
    reload = threading.Thread(target=registry.reload_declarative_feeds)
    reload.start()
    time.sleep(0.05)
    served = [_connect()[1]["feeds_query"]]                 # mid-reload: served at once
    server.LIVE.wait(10)
    reload.join()
    served.append(server.LIVE._descs["feeds_query"])
    _connect()
    server.LIVE.wait(10)
    served.append(_connect()[1]["feeds_query"])
    log("OUTPUT", [[n for n in ("race_a", "race_b", "race_c") if f"`{n}`" in t] for t in served])
    assert all(f"`{n}`" in t for t in served for n in ("race_a", "race_b"))
    assert "`race_c`" in served[-1] and server._signature() == server.LIVE._sig


def test_a_feed_another_worker_landed_is_read_before_a_call(feeds_dir, log):
    """Another worker lands a feed file and reloads its own registry only: here, the next tool
    call reads the folder first and the next refresh names the feed."""
    from fastmcp import Client
    from app.mcp import registry, server
    _connect()
    _land(feeds_dir, "other_worker_feed", "Feed another worker landed")
    assert "other_worker_feed" not in registry.FEEDS

    async def call():
        async with Client(server.mcp) as c:
            return (await c.call_tool("platform_capabilities", {"section": "feeds"})).structured_content
    reply = asyncio.run(call())
    assert "other_worker_feed" in registry.FEEDS and "other_worker_feed" in json.dumps(reply)
    _connect()
    server.LIVE.wait(10)
    text = _connect()[1]["feeds_query"]
    log("OUTPUT", text.split("\n\n")[0][-300:])
    assert "Feed another worker landed `other_worker_feed`" in text


# --------------------------------------------------------------------------- hazards in words
def test_a_place_qualified_hazard_is_said_in_words(monkeypatch, log):
    """A contributed hazard's stem names its place once, from the countries its row declares;
    one return period is said in the singular."""
    from app.graph.geo import tiffs
    from app.mcp import packs
    monkeypatch.setattr(tiffs, "catalog", lambda include_staged=True: {
        "hazard_flood": {}, "hazard_flood_rp10": {}, "hazard_flood_rp500": {},
        "hazard_earthquake_taiwan_rp475": {"title": "Earthquake hazard, 1-in-475 year PGA on rock, Taiwan",
                                           "countries": ["Taiwan"]},
        "hazard_cr_flood": {"title": "Flood depth (Fathom, 90 m)", "countries": ["Costa Rica"]}})
    words = packs._risk_hazards()
    log("OUTPUT", words)
    assert words == ["flood", "flood (Costa Rica)", "flood at 10- to 500-year return periods",
                     "earthquake (Taiwan) at a 475-year return period"]
