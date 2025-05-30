from pathlib import Path
from textwrap import dedent

import pytest

import faebryk.core.parameter as fab_param
import faebryk.library._F as F
from atopile import address, errors
from atopile.datatypes import ReferencePartType, TypeRef
from atopile.front_end import Bob, _has_ato_cmp_attrs
from atopile.parse import parse_text_as_file
from faebryk.libs.library import L
from faebryk.libs.picker.picker import DescriptiveProperties
from faebryk.libs.util import cast_assert


def _get_mif(node: L.Node, name: str, key: str | None = None) -> L.ModuleInterface:
    return cast_assert(
        L.ModuleInterface,
        _get_attr(node, name, key),
    )


def _get_attr(node: L.Node, name: str, key: str | None = None) -> L.Node:
    return Bob.get_node_attr(node, ReferencePartType(name, key))


def test_empty_module_build(bob: Bob):
    text = dedent(
        """
        module A:
            pass
        """
    )
    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))
    assert isinstance(node, L.Module)
    assert isinstance(node, bob.modules[address.AddrStr(":A")])


def test_simple_module_build(bob: Bob):
    text = dedent(
        """
        module A:
            a = 1
        """
    )
    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))
    assert isinstance(node, L.Module)

    param = node.runtime["a"]
    assert isinstance(param, fab_param.ParameterOperatable)
    # TODO: check value


def test_arithmetic(bob: Bob):
    text = dedent(
        """
        module A:
            a = 1 to 2 * 3
            b = a + 4
        """
    )
    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))
    assert isinstance(node, L.Module)

    # TODO: check output
    # Requires params solver to be sane


def test_simple_new(bob: Bob):
    text = dedent(
        """
        component SomeComponent:
            signal a

        module A:
            child = new SomeComponent
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))

    assert isinstance(node, L.Module)
    child = _get_attr(node, "child")
    assert child.has_trait(_has_ato_cmp_attrs)

    a = _get_attr(child, "a")
    assert isinstance(a, F.Electrical)


def test_nested_nodes(bob: Bob):
    text = dedent(
        """
        interface SomeInterface:
            signal d
            signal e

        component SomeComponent:
            pin A1
            signal a
            a ~ A1
            signal b ~ pin 2
            signal c ~ pin "C3"

        module SomeModule:
            cmp = new SomeComponent
            intf = new SomeInterface

        module ChildModule from SomeModule:
            signal child_signal

        module A:
            child = new ChildModule
            intf = new SomeInterface
            intf ~ child.intf
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))

    assert isinstance(node, L.Module)


def test_resistor(bob: Bob, repo_root: Path):
    bob.search_paths.append(
        repo_root / "test" / "common" / "resources" / ".ato" / "modules"
    )

    text = dedent(
        """
        from "generics/resistors.ato" import Resistor

        component ResistorB from Resistor:
            footprint = "R0805"

        module A:
            r1 = new ResistorB
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))

    assert isinstance(node, L.Module)

    r1 = _get_attr(node, "r1")
    assert r1.get_trait(F.has_package)._enum_set == {F.has_package.Package.R0805}


def test_standard_library_import(bob: Bob):
    text = dedent(
        """
        import Resistor
        from "interfaces.ato" import PowerAC

        module A:
            r1 = new Resistor
            power_in = new PowerAC
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))

    assert isinstance(node, L.Module)

    r1 = _get_attr(node, "r1")
    assert isinstance(r1, F.Resistor)

    assert _get_attr(node, "power_in")


@pytest.mark.parametrize(
    "import_stmt,class_name,pkg_str,pkg",
    [
        ("import Resistor", "Resistor", "R0402", F.has_package.Package.R0402),
        (
            "from 'generics/resistors.ato' import Resistor",
            "Resistor",
            "0402",
            F.has_package.Package.R0402,
        ),
        (
            "from 'generics/capacitors.ato' import Capacitor",
            "Capacitor",
            "0402",
            F.has_package.Package.C0402,
        ),
    ],
)
def test_reserved_attrs(
    bob: Bob,
    import_stmt: str,
    class_name: str,
    pkg_str: str,
    pkg: F.has_package.Package,
    repo_root: Path,
):
    bob.search_paths.append(
        repo_root / "test" / "common" / "resources" / ".ato" / "modules"
    )

    text = dedent(
        f"""
        {import_stmt}

        module A:
            a = new {class_name}
            a.package = "{pkg_str}"
            a.mpn = "1234567890"
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["A"]))

    assert isinstance(node, L.Module)

    a = _get_attr(node, "a")
    assert a.get_trait(F.has_package)._enum_set == {pkg}
    assert a.get_trait(F.has_descriptive_properties).get_properties() == {
        DescriptiveProperties.partno: "1234567890"
    }


def test_import_ato(bob: Bob, tmp_path):
    tmp_path = Path(tmp_path)
    some_module_search_path = tmp_path / "path"
    some_module_path = some_module_search_path / "to" / "some_module.ato"
    some_module_path.parent.mkdir(parents=True)

    some_module_path.write_text(
        dedent(
            """
        import Resistor

        module SpecialResistor from Resistor:
            footprint = "R0805"
        """
        ),
        encoding="utf-8",
    )

    top_module_content = dedent(
        """
        from "to/some_module.ato" import SpecialResistor

        module A:
            r1 = new SpecialResistor
        """
    )

    bob.search_paths.append(some_module_search_path)

    tree = parse_text_as_file(top_module_content)
    node = bob.build_ast(tree, TypeRef(["A"]))

    assert isinstance(node, L.Module)

    r1 = _get_attr(node, "r1")
    assert isinstance(r1, F.Resistor)


@pytest.mark.parametrize(
    "module,count", [("A", 1), ("B", 3), ("C", 5), ("D", 6), ("E", 6)]
)
def test_traceback(bob: Bob, module: str, count: int):
    text = dedent(
        """
        module A:
            doesnt_exit ~ notta_connectable

        module B:
            a = new A

        module C:
            b = new B

        module D from C:
            pass

        module E from D:
            pass
        """
    )

    tree = parse_text_as_file(text)

    with pytest.raises(errors.UserKeyError) as e:
        bob.build_ast(tree, TypeRef([module]))

    assert e.value.traceback is not None
    assert len(e.value.traceback) == count


# TODO: test connect
# - signal ~ signal
# - higher-level mif
# - duck-typed
def test_signal_connect(bob: Bob):
    text = dedent(
        """
        module App:
            signal a
            signal b
            signal c
            a ~ b
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)

    a = _get_mif(node, "a")
    b = _get_mif(node, "b")
    c = _get_mif(node, "c")

    assert a.is_connected_to(b)
    assert not a.is_connected_to(c)


def test_interface_connect(bob: Bob):
    text = dedent(
        """
        interface SomeInterface:
            signal one
            signal two

        module App:
            a = new SomeInterface
            b = new SomeInterface
            c = new SomeInterface
            a ~ b
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)

    a = _get_mif(node, "a")
    b = _get_mif(node, "b")
    c = _get_mif(node, "c")

    assert a.is_connected_to(b)
    assert not a.is_connected_to(c)

    a_one = _get_mif(a, "one")
    b_one = _get_mif(b, "one")
    c_one = _get_mif(c, "one")
    a_two = _get_mif(a, "two")
    b_two = _get_mif(b, "two")
    c_two = _get_mif(c, "two")

    assert a_one.is_connected_to(b_one)
    assert a_two.is_connected_to(b_two)
    assert not any(
        a_one.is_connected_to(other) for other in [a_two, b_two, c_one, c_two]
    )
    assert not any(
        a_two.is_connected_to(other) for other in [a_one, b_one, c_one, c_two]
    )


def test_duck_type_connect(bob: Bob):
    text = dedent(
        """
        interface SomeInterface:
            signal one
            signal two

        interface SomeOtherInterface:
            signal one
            signal two

        module App:
            a = new SomeInterface
            b = new SomeOtherInterface
            a ~ b
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)

    a = _get_mif(node, "a")
    b = _get_mif(node, "b")

    a_one = _get_mif(a, "one")
    b_one = _get_mif(b, "one")
    a_two = _get_mif(a, "two")
    b_two = _get_mif(b, "two")

    assert a_one.is_connected_to(b_one)
    assert a_two.is_connected_to(b_two)
    assert not any(a_one.is_connected_to(other) for other in [a_two, b_two])
    assert not any(a_two.is_connected_to(other) for other in [a_one, b_one])


def test_shim_power(bob: Bob):
    from atopile.attributes import Power

    a = Power()
    b = F.ElectricPower()

    bob._connect(a, b, None)

    assert a.lv.is_connected_to(b.lv)
    assert a.hv.is_connected_to(b.hv)
    assert not a.lv.is_connected_to(b.hv)


def test_requires(bob: Bob):
    text = dedent(
        """
        module App:
            signal a
            signal b

            a.required = True
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)

    a = _get_mif(node, "a")
    assert a.has_trait(F.requires_external_usage)


def test_key(bob: Bob):
    text = dedent(
        """
        import Resistor
        module App:
            r = new Resistor
            signal a ~ r.unnamed[0]
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)

    r = _get_attr(node, "r")
    assert isinstance(r, F.Resistor)


def test_pin_ref(bob: Bob):
    text = dedent(
        """
        module Abc:
            pin 1 ~ signal b

        module App:
            abc = new Abc
            signal a ~ abc.1
        """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)


def test_non_ex_pin_ref(bob: Bob):
    text = dedent(
        """
        import Resistor
        module App:
            r = new Resistor
            signal a ~ r.unnamed[2]
        """
    )

    tree = parse_text_as_file(text)
    with pytest.raises(errors.UserKeyError):
        bob.build_ast(tree, TypeRef(["App"]))


def test_regression_pin_refs(bob: Bob):
    text = dedent(
        """
        import ElectricPower
        component App:
            signal CNT ~ pin 3
            signal NP ~ pin 5
            signal VIN_ ~ pin 2
            signal VINplus ~ pin 1
            signal VO_ ~ pin 4
            signal VOplus ~ pin 6

            power_in = new ElectricPower
            power_out = new ElectricPower

            power_in.vcc ~ pin 1
            power_in.gnd ~ pin 2
            power_out.vcc ~ pin 6
            power_out.gnd ~ pin 4
    """
    )

    tree = parse_text_as_file(text)
    node = bob.build_ast(tree, TypeRef(["App"]))

    assert isinstance(node, L.Module)
