# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

import logging
from pathlib import Path

import pytest

import faebryk.library._F as F  # noqa: F401  # This is required to prevent a circular import
from faebryk.libs.kicad.fileformats_latest import (
    C_effects,
    C_footprint,
    C_kicad_footprint_file,
    C_kicad_fp_lib_table_file,
    C_kicad_netlist_file,
    C_kicad_pcb_file,
    C_kicad_project_file,
)
from faebryk.libs.kicad.fileformats_sch import C_kicad_sch_file, C_kicad_sym_file
from faebryk.libs.kicad.fileformats_version import kicad_footprint_file
from faebryk.libs.sexp.dataclass_sexp import JSON_File, SEXP_File
from faebryk.libs.test.fileformats import (
    _FP_DIR,
    _FPLIB_DIR,  # noqa: F401
    _NETLIST_DIR,  # noqa: F401
    _PCB_DIR,  # noqa: F401
    _PRJ_DIR,  # noqa: F401
    _SCH_DIR,  # noqa: F401
    _SYM_DIR,  # noqa: F401
    _VERSION_DIR,  # noqa: F401
    DEFAULT_VERSION,  # noqa: F401
    FPFILE,
    FPLIBFILE,
    NETFILE,
    PCBFILE,
    PRJFILE,
    SCHFILE,
    SYMFILE,
)
from faebryk.libs.util import ConfigFlag, find, not_none

logger = logging.getLogger(__name__)

DUMP = ConfigFlag("DUMP", descr="dump load->save into /tmp")


def test_parser_netlist():
    netlist = C_kicad_netlist_file.loads(NETFILE)
    assert [(c.ref, c.value) for c in netlist.export.components.comps][:10] == [
        ("C1", "10uF"),
        ("C2", "10uF"),
        ("C3", "10uF"),
        ("C4", "10uF"),
        ("C5", "22uF"),
        ("C6", "100nF"),
        ("C7", "100nF"),
        ("C8", "10uF"),
        ("C9", "100nF"),
        ("C10", "100nF"),
    ]


def test_parser_project():
    pro = C_kicad_project_file.loads(PRJFILE)
    assert pro.pcbnew.last_paths.netlist == "../../faebryk/faebryk.net"


def test_parser_schematics():
    sch = C_kicad_sch_file.loads(SCHFILE)
    assert sch.kicad_sch.lib_symbols.symbols["power:GND"].power is not None
    assert sch.kicad_sch.lib_symbols.symbols["Device:R"].power is None
    assert (
        sch.kicad_sch.lib_symbols.symbols["Amplifier_Audio:LM4990ITL"]
        .propertys["Datasheet"]
        .value
        == "http://www.ti.com/lit/ds/symlink/lm4990.pdf"
    )


def test_parser_symbols():
    sym = C_kicad_sym_file.loads(SYMFILE)
    assert (
        sym.kicad_symbol_lib.symbols["AudioJack-CUI-SJ-3523-SMT"].name
        == "AudioJack-CUI-SJ-3523-SMT"
    )


def test_parser_pcb_and_footprints():
    pcb = C_kicad_pcb_file.loads(PCBFILE)
    fp = C_kicad_footprint_file.loads(FPFILE)

    assert [f.name for f in pcb.kicad_pcb.footprints] == [
        "logos:faebryk_logo",
        "lcsc:LED0603-RD-YELLOW",
        "lcsc:R0402",
        "lcsc:BAT-TH_BS-02-A1AJ010",
    ]

    assert not pcb.kicad_pcb.setup.pcbplotparams.usegerberextensions

    padtype = pcb.C_kicad_pcb.C_pcb_footprint.C_pad.E_type
    assert [(p.name, p.type) for p in fp.footprint.pads] == [
        ("", padtype.smd),
        ("", padtype.smd),
        ("1", padtype.smd),
        ("2", padtype.smd),
    ]

    logo_fp = find(pcb.kicad_pcb.footprints, lambda f: f.name == "logos:faebryk_logo")
    assert C_footprint.E_attr.exclude_from_bom in logo_fp.attr


def test_write():
    pcb = C_kicad_pcb_file.loads(PCBFILE)

    def _d1(pcb: C_kicad_pcb_file):
        return find(
            pcb.kicad_pcb.footprints,
            lambda f: f.propertys["Reference"].value == "D1",
        )

    led_p = _d1(pcb).propertys["Value"]
    assert led_p.value == "LED"
    led_p.value = "LED2"

    pcb_reload = C_kicad_pcb_file.loads(pcb.dumps())
    assert _d1(pcb_reload).propertys["Value"].value == "LED2"


def test_empty_enum_positional():
    pcb = C_kicad_pcb_file.loads(PCBFILE)

    def _b1_p1(pcb: C_kicad_pcb_file):
        return find(
            find(
                pcb.kicad_pcb.footprints,
                lambda f: f.propertys["Reference"].value == "B1",
            ).pads,
            lambda p: p.name == "1",
        )

    _b1_p1(pcb).drill = C_footprint.C_pad.C_drill(
        C_footprint.C_pad.C_drill.E_shape.stadium, 0.5, 0.4
    )

    def _effects(pcb: C_kicad_pcb_file):
        return (
            find(pcb.kicad_pcb.footprints, lambda f: f.name == "logos:faebryk_logo")
            .propertys["Datasheet"]
            .effects
        )

    _effects(pcb).justifys.append(
        C_effects.C_justify([C_effects.C_justify.E_justify.center_horizontal])
    )
    _effects(pcb).justifys.append(
        C_effects.C_justify([C_effects.C_justify.E_justify.top])
    )

    pcb_reload = C_kicad_pcb_file.loads(pcb.dumps())

    assert (
        not_none(_b1_p1(pcb_reload).drill).shape
        == C_footprint.C_pad.C_drill.E_shape.stadium
    )

    # empty center string ignored
    assert _effects(pcb).get_justifys() == [
        C_effects.C_justify.E_justify.center_horizontal,
        C_effects.C_justify.E_justify.top,
    ]


@pytest.mark.parametrize(
    ("parser", "path"),
    [
        (C_kicad_pcb_file, PCBFILE),
        (C_kicad_footprint_file, FPFILE),
        (C_kicad_netlist_file, NETFILE),
        (C_kicad_project_file, PRJFILE),
        (C_kicad_fp_lib_table_file, FPLIBFILE),
        (C_kicad_sch_file, SCHFILE),
        (C_kicad_sym_file, SYMFILE),
    ],
)
def test_dump_load_equality(parser: type[SEXP_File | JSON_File], path: Path):
    loaded = parser.loads(path)
    dump = loaded.dumps(Path("/tmp") / path.name if DUMP else None)
    loaded_dump = parser.loads(dump)
    dump2 = loaded_dump.dumps()
    assert dump == dump2


@pytest.mark.parametrize(
    "fp_path", _FP_DIR(5).glob("*.kicad_mod"), ids=lambda p: p.stem
)
def test_v5_fp_convert(fp_path: Path):
    fp = kicad_footprint_file(fp_path)
    assert fp.footprint.name.split(":")[-1] == fp_path.stem


@pytest.mark.parametrize(
    "fp_path", _FP_DIR(6).glob("*.kicad_mod"), ids=lambda p: p.stem
)
def test_v6_fp_convert(fp_path: Path):
    fp = kicad_footprint_file(fp_path)
    assert fp.footprint.name.split(":")[-1] == fp_path.stem
