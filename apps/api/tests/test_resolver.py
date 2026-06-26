"""S6 — the L1-vs-L2 resolver. All fast: it reasons from the catalog (drive_tifs) +
conf, no network. Default preference is L1 (precomputed risk exists); L2 when forced
or when no L1 exists; the hazard<->risk name map handles landslides/landslide (R5).
"""
from app.graph.geo import resolver


def test_flood_defaults_to_l1_with_l2_available():
    """S6.T1 [fast] flood: both L1 and L2 exist -> default to L1 (precomputed); L2 noted available."""
    p = resolver.resolve_layer("flood")
    assert p.level == 1
    assert p.oracle_tif == "risk_flood"
    assert p.l2_available is True


def test_forcing_l2_selects_inputs_and_weights():
    """S6.T2 [fast] requested_level=2 -> compute from hazard_flood x vuln, weights from conf."""
    p = resolver.resolve_layer("flood", requested_level=2)
    assert p.level == 2
    assert p.hazard_input == "hazard_flood"
    assert p.weights and abs(sum(p.weights.values()) - 1.0) < 1e-6
    assert set(p.vuln_inputs) == set(p.weights)
    assert p.oracle_tif == "risk_flood"          # L1 still available as the validation oracle


def test_all_hazards_buildable_at_l2_after_widening():
    """S6.T3 / S7 [fast] every catalogued hazard resolves: L1 by default, L2 when forced (S7 recipes)."""
    for h in ("flood", "cyclone", "earthquake", "landslide", "storm",
              "tsunami", "drought", "fire", "flashflood"):
        assert resolver.resolve_layer(h).level == 1                       # default L1 (precomputed risk)
        assert resolver.resolve_layer(h, requested_level=2).level == 2    # L2 buildable (S7 recipe)


def test_landslide_naming_mismatch_resolves():
    """S6.T4 [fast] hazard_landslides (plural) and risk_landslide (singular) both resolve to landslide L1."""
    for name in ("landslide", "hazard_landslides", "risk_landslide"):
        p = resolver.resolve_layer(name)
        assert p.hazard == "landslide"
        assert p.level == 1 and p.oracle_tif == "risk_landslide"


def test_unknown_hazard_is_unresolved():
    """[fast] an unknown hazard returns level None with a reason."""
    p = resolver.resolve_layer("volcano")
    assert p.level is None and p.missing
