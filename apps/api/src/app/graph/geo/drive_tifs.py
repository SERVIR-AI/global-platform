"""Complete Drive catalog: every tif from the ASEAN data sheet (`notes` tab) -> its
Google Drive file id. This is the single source of truth for *where a raster lives* —
what the risk pipeline downloads: hazard, vulnerability, raw, and risk layers.

`conf/tiffs.yml` is separate and narrower on purpose: it carries the human/LLM-facing
metadata (descriptions, legends, bands) for only the 9 chat-selectable hazard layers,
which the route node lists to the model. Here we list all of them, by filename, so any
layer can be fetched by name with gdown.

Ported from grp_mvp/hazards.py; kept in step with the `notes` tab.
"""

DRIVE_TIFS = {
    'hazard_flood.tif': '1s5ZSmFGLDH3i2u74RRq0MBOyawV3Fh34',
    'hazard_flashflood.tif': '1l88oCDFxkJgcO5Bzp8MkhYBtUblo06yK',
    'hazard_drought.tif': '1T5JDp2gkavG79yNFds-1w9gJs3-j6n7G',
    'hazard_fire.tif': '1n-7VtJKPUN1bfl6TB0a9cvixkC-V8w7b',
    'hazard_landslides.tif': '1A4AgHZuFI86BNAU92vokb1ZFQZuexCcb',
    'hazard_cyclone.tif': '1NNXdl7ZOwYLf08lii149M7eC0QaborAa',
    'hazard_storm.tif': '1BwEMxKze9XwXZL9n-KS5IX867ErRO9Qz',
    'hazard_tsunami.tif': '19kb42dkl4U1BlO8K_NTaPFw5BpxMFbMw',
    'hazard_earthquake.tif': '1ZoQYtROUxf_YVzj1BgCRNFsqSWALpBxZ',
    'risk_flood.tif': '1-jSs0snadjdHyreae-kkBz6Z5yMP2D4f',
    'risk_flashflood.tif': '1QojNJQuUETn9r1ZjgRV8G6VzWy-P45j4',
    'risk_drought.tif': '1MFx1ifPSiya3kmKdgl14U5dG2D2gUTAg',
    'risk_fire.tif': '19B47mNyF4GtcJQMLxAsXmTiOvrfeK_Eo',
    'risk_landslide.tif': '1yrbr-cMid7kQvEn0KiNYP5woedGT5ur4',
    'risk_cyclone.tif': '1roAWHxwkj5ac_MCvdYx9tmqEGqLWtxXw',
    'risk_storm.tif': '1yzCS89TlvZ9qglcE9AQeg9CBxTV2x7xK',
    'risk_tsunami.tif': '1p7NttM32PgHC5cewYp_k-3Cco_j2CrT3',
    'risk_earthquake.tif': '1U2cdmYBTqrvSBTEWwTr_bwctzRNbwKj-',
    'pop_all_total_raw_SEA.tif': '1MPa9Q4oRhYP31LKQ7BbcnbpKez5xziYU',
    'vulnerability_pop_all_total.tif': '12e5NHQ8-XAmGTruShRFliqo0C3iJe-4d',
    'M_infant_raw_SEA.tif': '1u9VAKF77Mjx3h8gTTC9HZT1CIKVAmhxp',
    'F_infant_raw_SEA.tif': '1fyV-boeOdUKo8TVc_EzvV_qB-Zq3XUyH',
    'M_15_60_raw_SEA.tif': '1odMa_XQJDkBdPdxpwDj8jISXFhaD8Lpm',
    'F_15_60_raw_SEA.tif': '1VGWzTV25Zis9U7m6cQsuKFlsCM-cYNUo',
    'M_above60_raw_SEA.tif': '1Yhl9Ec_opYYiELobhXAe7zN5gY3rCJnb',
    'F_above60_raw_SEA.tif': '1zbugH8jY50VI58YO8pIDEXuezU2lsRE-',
    'vulnerability_M_infant.tif': '1Rv5YD2_OcKdozLyx6-i6Y4UYO3UfgqzS',
    'vulnerability_F_infant.tif': '119ftD8HKAiy7gqJqlYaASLDPTtnOQNh0',
    'vulnerability_M_15_60.tif': '1ZrAzlmxcbMv_4ZRcKFVDaTY5Xp01x0NY',
    'vulnerability_F_15_60.tif': '1_QqykE14W5NoYVqjSdmwYhAc9rZYrCza',
    'vulnerability_M_above60.tif': '1g0S1yLZ0_t6c_MLChBI8j8trMtfYQY_H',
    'vulnerability_F_above60.tif': '17q9gJ8i-iLSZt8NIVi1_qWNmMDLUdXi1',
    'bldDensity.tif': '1JfycZhCTZjsqo4YuvXRV51-6muWlshOg',
    'bldHeight.tif': '17n534ntkSEnb8JzURnyBcKtMU99_v_VG',
    'vulnerability_reclass_blddensity.tif': '1Ekr5mY0N8bFFO7FgWyElXQdMtaDqxB_z',
    'vulnerability_reclass_bldheight.tif': '1dO5eGCxFoX-9qHVW3ifPMn0p_vTX_Q0k',
    'roadDistance.tif': '1xjoarZQQXvyyqi4Tevsk8M6wXjAwKtM3',
    'vulnerability_reclass_road.tif': '1qF-0VobsVDpJsKIE2DrQP98nkEIuY7BA',
    'vulnerability_hospital_costDistance.tif': '1ayNWwtF9jSEyeNtRDrryZqVJ517fHIb2',
    'vulnerability_education_CostDistance_100m.tif': '1zMBwm-fyuln3-R0-u4D9bbtueGYuN3Kv',
    'vulnerability_shelter_CostDistance_100m.tif': '1WRZ8MPQsSF-THy3MC6t2BEHOCCyQKQX6',
    'vulnerability_reclass_wdpa.tif': '14xhiy-R1dfTxauntkTcNUmA-N_frq2FE',
    'vulnerability_reclass_tcc.tif': '1KLpqLas0mvN93E2Ud37YbKU6PG9Ls4sO',
    'vulnerability_raw_lai.tif': '1VVN3caQs-f21OPs8d6fEurLLAZR1X_yT',
    'vulnerability_reclass_lai.tif': '1YiOtPzaLHnfsfpyx6lWtx5E6Yg9b3d8Q',
    'eviMean2024.tif': '1VAGI2mQTp5rl7wW6QOeotmJv3L20_asX',
    'vulnerability_raw_gdp.tif': '136SFc3Q3dde-UCPTkKoalV4Io90I-3GW',
    'vulnerability_reclass_gdp.tif': '1ldFWpn_dmZ656inscN3MjpJwhLK14LnY',
    'RLCMS_2024_SEA.tif': '10WLKc7EemfiriHNRueYSvaBfTBYh4vzg',
    'global_pc_h100glob.tif': '1nagYbWbc3B1lqX6gdQfuteMP9cIFgVKg',
    'global_pc_h100glob_class.tif': '1bVr7K8rhJ6nROHr8kIl-rVabD5TY4Yb4',
    'Wind_CC_T100.tif': '1wDiKeXH00gr7kBH-XvRYYUatNspDw7QP',
    'PGA_250y.tif': '1v--pNkz5SQkvsP54QGTok4rYq5UOP4q3',
    'raw_storm.tif': '1YrOE_qBSXsquZDcLhj5Lvc3sJCeDdpMZ',
    'raw_tsunami.tif': '1nQEfkcDVwSqfw13ZEzj1pys-ZIVSCfie',
    'raw_fire.tif': '1EcdpclrZaxQvZdJbdbFKiYLrTNn4kJ2k',
    'n1_mosaic_wgs84_opt.tif': '1_8GKfE77rzIdf0g-hbXalHQG8PsnBYkX',
    'Stable_forest_SEA.tif': '1odbAuA3enzaylq4kDmBS28FjWSdGWhIN',
    'StableNonForest_forest_SEA.tif': '10WuyK0D5_6NNFEJM1AEozlm1Z7bwYdCm',
    'Potential_Loss_forest_SEA.tif': '10NHDg1l-qCP7O6MyR-w0qA7aY_4NAtHz',
    'Potential_Gain_forest_SEA.tif': '1KZRO_3I-v4vhlaMpq5tqObqnk0_UBRsf',
    'Mangrove_Stable_SEA.tif': '1qKLMLCthi7Dy8cIE080fCd6lYg8MCRQF',
    'Mangrove_Stable_Non_Mangrove_SEA.tif': '1071nKP8492sDkO6boEZZKM56ZMfXAlG2',
    'Mangrove_Potential_Loss_SEA.tif': '1wU7HDjSfkVMu_MUm1lLyQOx_nJOcNwb7',
    'Mangrove_Potential_Gain_SEA.tif': '1ZzSEasxv2w6dNmx_Fm5s-AOEgh9UqBmy',
}


def drive_id(name):
    """Drive id for a tif by filename ('hazard_flood.tif') or stem ('hazard_flood')."""
    if name in DRIVE_TIFS:
        return DRIVE_TIFS[name]
    return DRIVE_TIFS.get(f"{name}.tif")


def filename(name):
    """Canonical '<name>.tif' for a catalog entry given a filename or stem, or None."""
    if name in DRIVE_TIFS:
        return name
    fn = f"{name}.tif"
    return fn if fn in DRIVE_TIFS else None
