"""Sprint fix round 3, retrieval (2026-10-19): a table whose chunk ends in a long
'Sources: … et al., 1975; …' footnote is a table, not a reference list. Shaped on the
Wetlands Supplement chunks the land-cover library passed over (Table 4.3 above-ground
biomass, Table 4.11 soil carbon): their layout and figures as extracted, the source
names replaced by placeholders. Bibliography and contents pages are still passed over."""
import pytest

from app.mcp import fetch
from app.rag import filters as rag_filters

from test_sprint_fix2_review import library  # noqa: F401  (fixture: a no-network library)

_SOURCES = ("2Sources: Alpha et al., 1975; Beta, 1978; Gamma et al., 1982; Delta and Epsilon, 1986; Zeta et al., "
            "1986; Eta et al., 1987; 1988, \n2000, 2008; Theta et al., 1990; Iota et al., 1991; Kappa and Lambda, "
            "1992; Mu et al., 1992; Nu et al., 1996; Xi \net al., 1998; Omicron and Pi, 2001; Rho, 2003; Sigma et "
            "al., 2003; Tau and Upsilon, 2004; Phi et al., 2006; Chi et al., \n2008; Psi et al., 2008; Omega et "
            "al., 2011; Alpha et al., 2011; Beta and Gamma, 2011 \n3Sources: Alpha et al, 1962; Beta, 1977; Gamma "
            "and Delta, 1983; Epsilon et al., 1995; Zeta et al., 2003; Eta and Theta, 2008; \nIota et al., 2009 \n"
            "4Sources: Kappa and Lambda, 1974; Mu, 1984; Nu, 1990; Xi, 1993; Omicron et al., 1995; Pi, 1997; Rho "
            "et al., 2001; \nSigma et al., 2004; Tau et al., 2006; Upsilon et al., 2008; Phi et al., 2008; Chi et "
            "al., 2012 \n595% CI of the geometric mean")
_ROWS_43 = ("Domain Region Above-ground biomass 95% CI5 Range n \nTropical \nTropical Wet 1922 187, 204 8.7-384 49 \n"
            "Tropical Dry 923 88, 97 3.2-201 13 \nSubtropical  754 66, 84 3.9-129 10 \n")
TABLE_43 = ("4.14 Supplement to the Guidelines for National Greenhouse Gas Inventories: Wetlands \nTABLE 4.3  \n"
            "ABOVE-GROUND BIOMASS IN MANGROVES (TONNES D.M. HA-1)\n 1 \n" + _ROWS_43 +
            "1This Table provides supplementary values to those presented in Table 4.7 -4.9, Chapter 4, Volume 4 "
            "of the Guidelines. \n" + _SOURCES)
TABLE_411 = (
    "When deriving global emission factors, uncertainties can also be \nintroduced by areas where there is "
    "greater prevalence of data from specific regions of the globe.\n\nChapter 4: Coastal Wetlands\n\n"
    "4.26 Supplement to the Guidelines for National Greenhouse Gas Inventories: Wetlands \nTABLE 4.11 \n"
    "SOIL CARBON STOCKS FOR MANGROVES, TIDAL MARSHES AND SEAGRASS MEADOWS FOR EXTRACTION ACTIVITIES \n"
    "ORGANIC SOILS (TONNES C HA-1) \nVegetation type SOBEFORE 95% CI1 range n \nMangrove 4712 436, 510 216-935 43 \n"
    "Tidal marsh 3403 315, 366 221-579 35 \nSeagrass meadow NA4 \nMINERAL SOILS (TONNES C HA-1) \n"
    "Vegetation type SOBEFORE 95% CI1 range n \nMangrove  2865 247, 330 55-1376 77 \n"
    "Tidal marsh 2266 202, 252 15.6-623 82 \nSeagrass meadow7 1088 84, 139 9.1-829 89 \n"
    "AGGREGATED ORGANIC AND MINERAL SOILS (TONNES C HA-1) \nVegetation type SOBEFORE 95% CI1 range n \n"
    "Mangrove 386 351, 424 55-1376 119 \nTidal marsh 255 254, 297 15.6-623 117 \n195% CI of the geometric mean \n"
    "2Sources: Alpha et al., 2012; Beta et al., 2012; Gamma et al., 2003; Delta et al., 2011; Epsilon et al., "
    "2011; Zeta \net al., 2011; Eta et al., 2010 \n3Sources: Theta et al., 1999; Iota et al., 1996; Kappa et al., "
    "2012; Lambda and Mu, 2004; Nu et al., 1988; Xi, \n2007; Omicron et al., 2004; Pi and Rho, 1991; Sigma et al., "
    "1998; Tau et al., 1998; Upsilon and Phi, \n1980 \n4Seagrass meadows are assumed to be on mineral soils.")
UNCAPTIONED = _ROWS_43 + _SOURCES             # a table cut from its caption by the chunker
BIBLIOGRAPHY = (
    "Supplement to the Guidelines for National Greenhouse Gas Inventories: Wetlands 4.47 \n"
    "Alpha, J. W. (2003) Morphometric variability and allometric relationships in the seagrass. \nIn: University. \n"
    "Beta, Z., Gamma, J., Delta, J., Epsilon, L., Zeta, C. M. & Eta, M. D. (2002) Experimental \nevaluation of "
    "sediment conditions on seagrass. Journal of Marine Biology and Ecology 279(1-2): 73-87. \n"
    "Theta, J. A. (1998) Nitrogen biogeochemistry of aquaculture ponds. Aquaculture 166: 181-212. \n"
    "Iota, R. J., Kappa, E. C., Lambda, E. M. & Mu, B. D. (2010) The Effects of Reduced Tidal Flushing \non "
    "Mangrove Structure and Function. Estuaries and Coasts 33(5): 1176-1185. \n"
    "Nu, J. A., Xi, M., Omicron, O., Pi, S. J. & Rho, D. D. (2012) Greenhouse gas fluxes from drained \n"
    "agricultural peatlands. Agriculture Ecosystems & Environment 150: 1-18. \n"
    "Sigma, R. S., Tau, R. D. & Upsilon, W. H. (1983) Sedimentation, accretion, and subsidence in marshes. "
    "\nLimnology and Oceanography 28(3): 494-502. \nPhi, A. B., Chi, J. W. & Psi, P. M. (2007) Small-scale "
    "heterogeneity in seagrass sediments. \nConservation 29 (3): 331-349. \nBotany 89 (2008) 128-137. \n"
    "Wetlands 31 (2): 343-352. \nVol. 12, No. 3, pp. 45-67, 2003")
LIST_OF_TABLES = (
    "Tables  \nTable 4.1 Specific management activities in coastal wetlands ................................ 6 \n"
    "Table 4.2 Carbon fraction of above-ground biomass in mangroves ........ 13 \n"
    "Table 4.3 Above-ground biomass in mangroves (tonnes d.m. ha-1) ................................ 14 \n"
    "Table 4.4 Above-ground biomass growth in mangroves (tonnes d.m. ha-1 yr-1) .............. 14 \n"
    "Table 4.5 Ratio of below-ground biomass to above-ground biomass (R) in mangroves .......... 14")


def test_a_table_with_a_sources_footnote_is_not_a_reference_list(log):
    """Table 4.3 and 4.11 shapes (captioned, numeric rows, long 'Sources:' footnotes) and
    the same rows cut from their caption are kept; the document's bibliography page and
    its list of tables are still passed over."""
    for name, text in (("TABLE 4.3 + sources", TABLE_43), ("TABLE 4.11 + sources", TABLE_411),
                       ("numeric rows + sources, no caption", UNCAPTIONED)):
        log("CHECK", f"kept: {name}")
        assert not rag_filters.looks_like_references(text), name
    for name, text in (("bibliography page", BIBLIOGRAPHY), ("list of tables", LIST_OF_TABLES),
                       ("sources footnote alone", _SOURCES)):
        log("CHECK", f"passed over: {name}")
        assert rag_filters.looks_like_references(text), name


DOCS = ((TABLE_43, {"title": "Wetlands Supplement, Table 4.3"}),
        (TABLE_411, {"title": "Wetlands Supplement, Table 4.11"}),
        (BIBLIOGRAPHY, {"title": "Wetlands Supplement, references"}),
        (LIST_OF_TABLES, {"title": "Wetlands Supplement, contents"}))
Q43 = "Table 4.3 above-ground biomass in mangroves tonnes d.m. ha tropical wet tropical dry subtropical"


@pytest.mark.parametrize("pack", ["land-cover", "risk"])
def test_an_opted_in_library_returns_the_tables(library, pack, log):  # noqa: F811
    """Opted in, every pack's library returns the two tables (Table 4.3 first for its own
    words) and passes over only the bibliography and the list of tables."""
    c = library(pack, DOCS)
    skipped = {}
    hits = c.search(Q43, k=10, skip_references=True, skipped=skipped)
    titles = [h["metadata"]["title"] for h in hits]
    log("OUTPUT", f"{pack}: {titles}; skipped {skipped.get('references')}")
    assert titles[0] == "Wetlands Supplement, Table 4.3"
    assert set(titles) == {"Wetlands Supplement, Table 4.3", "Wetlands Supplement, Table 4.11"}
    assert sorted(skipped["references"]) == ["Wetlands Supplement, contents", "Wetlands Supplement, references"]


def test_corpus_search_returns_a_freshly_contributed_table(library, log):  # noqa: F811
    """corpus_search opts in: a contributed table with a sources footnote is returned, the
    contributed bibliography is passed over and the reply says so."""
    library("food-security", ((TABLE_411, {"title": "Contributed soil carbon table", "contribution_id": "c0ffee03"}),
                              (BIBLIOGRAPHY, {"title": "Contributed references", "contribution_id": "c0ffee04"})))
    out = fetch.search("soil carbon stocks mangroves tidal marshes tonnes C ha organic mineral", k=10)
    titles = [h["passport"]["title"] for h in out["hits"]]
    log("OUTPUT", f"{titles}; {out.get('passed_over')}")
    assert titles == ["Contributed soil carbon table"]
    assert "1 reference-list or contents passage(s)" in out["passed_over"]
