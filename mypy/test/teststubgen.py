from __future__ import annotations

import io
import os.path
import re
import shutil
import sys
import tempfile
import unittest
from types import ModuleType
from typing import Any, Tuple, TypeVar, cast
from typing_extensions import ParamSpec, TypeVarTuple

import pytest

from mypy.errors import CompileError
from mypy.moduleinspect import InspectError, ModuleInspect
from mypy.stubdoc import (
    ArgSig,
    FunctionSig,
    build_signature,
    find_unique_signatures,
    infer_arg_sig_from_anon_docstring,
    infer_prop_type_from_docstring,
    infer_sig_from_docstring,
    is_valid_type,
    parse_all_signatures,
    parse_signature,
)
from mypy.stubgen import (
    Options,
    collect_build_targets,
    generate_stubs,
    is_blacklisted_path,
    is_non_library_module,
    mypy_options,
    parse_options,
)
from mypy.stubgenc import InspectionStubGenerator, infer_c_method_args
from mypy.stubutil import (
    ClassInfo,
    common_dir_prefix,
    generate_inline_generic,
    infer_method_ret_type,
    remove_misplaced_type_comments,
    walk_packages,
)
from mypy.test.data import DataDrivenTestCase, DataSuite
from mypy.test.helpers import assert_equal, assert_string_arrays_equal, local_sys_path_set


class StubgenCmdLineSuite(unittest.TestCase):
    """Test cases for processing command-line options and finding files."""

    @unittest.skipIf(sys.platform == "win32", "clean up fails on Windows")
    def test_files_found(self) -> None:
        current = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                os.mkdir("subdir")
                self.make_file("subdir", "a.py")
                self.make_file("subdir", "b.py")
                os.mkdir(os.path.join("subdir", "pack"))
                self.make_file("subdir", "pack", "__init__.py")
                opts = parse_options(["subdir"])
                py_mods, pyi_mods, c_mods = collect_build_targets(opts, mypy_options(opts))
                assert_equal(pyi_mods, [])
                assert_equal(c_mods, [])
                files = {mod.path for mod in py_mods}
                assert_equal(
                    files,
                    {
                        os.path.join("subdir", "pack", "__init__.py"),
                        os.path.join("subdir", "a.py"),
                        os.path.join("subdir", "b.py"),
                    },
                )
            finally:
                os.chdir(current)

    @unittest.skipIf(sys.platform == "win32", "clean up fails on Windows")
    def test_packages_found(self) -> None:
        current = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                os.mkdir("pack")
                self.make_file("pack", "__init__.py", content="from . import a, b")
                self.make_file("pack", "a.py")
                self.make_file("pack", "b.py")
                opts = parse_options(["-p", "pack"])
                py_mods, pyi_mods, c_mods = collect_build_targets(opts, mypy_options(opts))
                assert_equal(pyi_mods, [])
                assert_equal(c_mods, [])
                files = {os.path.relpath(mod.path or "FAIL") for mod in py_mods}
                assert_equal(
                    files,
                    {
                        os.path.join("pack", "__init__.py"),
                        os.path.join("pack", "a.py"),
                        os.path.join("pack", "b.py"),
                    },
                )
            finally:
                os.chdir(current)

    @unittest.skipIf(sys.platform == "win32", "clean up fails on Windows")
    def test_module_not_found(self) -> None:
        current = os.getcwd()
        captured_output = io.StringIO()
        sys.stdout = captured_output
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                self.make_file(tmp, "mymodule.py", content="import a")
                opts = parse_options(["-m", "mymodule"])
                collect_build_targets(opts, mypy_options(opts))
                assert captured_output.getvalue() == ""
            finally:
                sys.stdout = sys.__stdout__
                os.chdir(current)

    def make_file(self, *path: str, content: str = "") -> None:
        file = os.path.join(*path)
        with open(file, "w") as f:
            f.write(content)

    def run(self, result: Any | None = None) -> Any | None:
        with local_sys_path_set():
            return super().run(result)


class StubgenCliParseSuite(unittest.TestCase):
    def test_walk_packages(self) -> None:
        with ModuleInspect() as m:
            assert_equal(set(walk_packages(m, ["mypy.errors"])), {"mypy.errors"})

            assert_equal(
                set(walk_packages(m, ["mypy.errors", "mypy.stubgen"])),
                {"mypy.errors", "mypy.stubgen"},
            )

            all_mypy_packages = set(walk_packages(m, ["mypy"]))
            self.assertTrue(
                all_mypy_packages.issuperset(
                    {"mypy", "mypy.errors", "mypy.stubgen", "mypy.test", "mypy.test.helpers"}
                )
            )


class StubgenUtilSuite(unittest.TestCase):
    """Unit tests for stubgen utility functions."""

    def test_parse_signature(self) -> None:
        self.assert_parse_signature("func()", ("func", [], []))

    def test_parse_signature_with_args(self) -> None:
        self.assert_parse_signature("func(arg)", ("func", ["arg"], []))
        self.assert_parse_signature("do(arg, arg2)", ("do", ["arg", "arg2"], []))

    def test_parse_signature_with_optional_args(self) -> None:
        self.assert_parse_signature("func([arg])", ("func", [], ["arg"]))
        self.assert_parse_signature("func(arg[, arg2])", ("func", ["arg"], ["arg2"]))
        self.assert_parse_signature("func([arg[, arg2]])", ("func", [], ["arg", "arg2"]))

    def test_parse_signature_with_default_arg(self) -> None:
        self.assert_parse_signature("func(arg=None)", ("func", [], ["arg"]))
        self.assert_parse_signature("func(arg, arg2=None)", ("func", ["arg"], ["arg2"]))
        self.assert_parse_signature('func(arg=1, arg2="")', ("func", [], ["arg", "arg2"]))

    def test_parse_signature_with_qualified_function(self) -> None:
        self.assert_parse_signature("ClassName.func(arg)", ("func", ["arg"], []))

    def test_parse_signature_with_kw_only_arg(self) -> None:
        self.assert_parse_signature(
            "ClassName.func(arg, *, arg2=1)", ("func", ["arg", "*"], ["arg2"])
        )

    def test_parse_signature_with_star_arg(self) -> None:
        self.assert_parse_signature("ClassName.func(arg, *args)", ("func", ["arg", "*args"], []))

    def test_parse_signature_with_star_star_arg(self) -> None:
        self.assert_parse_signature("ClassName.func(arg, **args)", ("func", ["arg", "**args"], []))

    def assert_parse_signature(self, sig: str, result: tuple[str, list[str], list[str]]) -> None:
        assert_equal(parse_signature(sig), result)

    def test_build_signature(self) -> None:
        assert_equal(build_signature([], []), "()")
        assert_equal(build_signature(["arg"], []), "(arg)")
        assert_equal(build_signature(["arg", "arg2"], []), "(arg, arg2)")
        assert_equal(build_signature(["arg"], ["arg2"]), "(arg, arg2=...)")
        assert_equal(build_signature(["arg"], ["arg2", "**x"]), "(arg, arg2=..., **x)")

    def test_parse_all_signatures(self) -> None:
        assert_equal(
            parse_all_signatures(
                [
                    "random text",
                    ".. function:: fn(arg",
                    ".. function:: fn()",
                    "  .. method:: fn2(arg)",
                ]
            ),
            ([("fn", "()"), ("fn2", "(arg)")], []),
        )

    def test_find_unique_signatures(self) -> None:
        assert_equal(
            find_unique_signatures(
                [
                    ("func", "()"),
                    ("func", "()"),
                    ("func2", "()"),
                    ("func2", "(arg)"),
                    ("func3", "(arg, arg2)"),
                ]
            ),
            [("func", "()"), ("func3", "(arg, arg2)")],
        )

    def test_infer_sig_from_docstring(self) -> None:
        assert_equal(
            infer_sig_from_docstring("\nfunc(x) - y", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x")], ret_type="Any")],
        )
        assert_equal(
            infer_sig_from_docstring("\nfunc(x)", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x")], ret_type="Any")],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x, Y_a=None)", "func"),
            [
                FunctionSig(
                    name="func",
                    args=[ArgSig(name="x"), ArgSig(name="Y_a", default=True)],
                    ret_type="Any",
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x, Y_a=3)", "func"),
            [
                FunctionSig(
                    name="func",
                    args=[ArgSig(name="x"), ArgSig(name="Y_a", default=True)],
                    ret_type="Any",
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x, Y_a=[1, 2, 3])", "func"),
            [
                FunctionSig(
                    name="func",
                    args=[ArgSig(name="x"), ArgSig(name="Y_a", default=True)],
                    ret_type="Any",
                )
            ],
        )

        assert_equal(infer_sig_from_docstring("\nafunc(x) - y", "func"), [])
        assert_equal(infer_sig_from_docstring("\nfunc(x, y", "func"), [])
        assert_equal(
            infer_sig_from_docstring("\nfunc(x=z(y))", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x", default=True)], ret_type="Any")],
        )

        assert_equal(infer_sig_from_docstring("\nfunc x", "func"), [])
        # Try to infer signature from type annotation.
        assert_equal(
            infer_sig_from_docstring("\nfunc(x: int)", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x", type="int")], ret_type="Any")],
        )
        assert_equal(
            infer_sig_from_docstring("\nfunc(x: int=3)", "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="int", default=True)], ret_type="Any"
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x=3)", "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type=None, default=True)], ret_type="Any"
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc() -> int", "func"),
            [FunctionSig(name="func", args=[], ret_type="int")],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x: int=3) -> int", "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="int", default=True)], ret_type="int"
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x: int=3) -> int   \n", "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="int", default=True)], ret_type="int"
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x: Tuple[int, str]) -> str", "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="Tuple[int,str]")], ret_type="str"
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring(
                "\nfunc(x: Tuple[int, Tuple[str, int], str], y: int) -> str", "func"
            ),
            [
                FunctionSig(
                    name="func",
                    args=[
                        ArgSig(name="x", type="Tuple[int,Tuple[str,int],str]"),
                        ArgSig(name="y", type="int"),
                    ],
                    ret_type="str",
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x: foo.bar)", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x", type="foo.bar")], ret_type="Any")],
        )

        assert_equal(
            infer_sig_from_docstring("\nfunc(x: list=[1,2,[3,4]])", "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="list", default=True)], ret_type="Any"
                )
            ],
        )

        assert_equal(
            infer_sig_from_docstring('\nfunc(x: str="nasty[")', "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="str", default=True)], ret_type="Any"
                )
            ],
        )

        assert_equal(infer_sig_from_docstring("\nfunc[(x: foo.bar, invalid]", "func"), [])

        assert_equal(
            infer_sig_from_docstring("\nfunc(x: invalid::type<with_template>)", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x", type=None)], ret_type="Any")],
        )

        assert_equal(
            infer_sig_from_docstring('\nfunc(x: str="")', "func"),
            [
                FunctionSig(
                    name="func", args=[ArgSig(name="x", type="str", default=True)], ret_type="Any"
                )
            ],
        )

    def test_infer_sig_from_docstring_duplicate_args(self) -> None:
        assert_equal(
            infer_sig_from_docstring("\nfunc(x, x) -> str\nfunc(x, y) -> int", "func"),
            [FunctionSig(name="func", args=[ArgSig(name="x"), ArgSig(name="y")], ret_type="int")],
        )

    def test_infer_sig_from_docstring_bad_indentation(self) -> None:
        assert_equal(
            infer_sig_from_docstring(
                """
            x
              x
             x
            """,
                "func",
            ),
            None,
        )

    def test_infer_arg_sig_from_anon_docstring(self) -> None:
        assert_equal(
            infer_arg_sig_from_anon_docstring("(*args, **kwargs)"),
            [ArgSig(name="*args"), ArgSig(name="**kwargs")],
        )

        assert_equal(
            infer_arg_sig_from_anon_docstring(
                "(x: Tuple[int, Tuple[str, int], str]=(1, ('a', 2), 'y'), y: int=4)"
            ),
            [
                ArgSig(name="x", type="Tuple[int,Tuple[str,int],str]", default=True),
                ArgSig(name="y", type="int", default=True),
            ],
        )

    def test_infer_prop_type_from_docstring(self) -> None:
        assert_equal(infer_prop_type_from_docstring("str: A string."), "str")
        assert_equal(infer_prop_type_from_docstring("Optional[int]: An int."), "Optional[int]")
        assert_equal(
            infer_prop_type_from_docstring("Tuple[int, int]: A tuple."), "Tuple[int, int]"
        )
        assert_equal(infer_prop_type_from_docstring("\nstr: A string."), None)

    def test_infer_sig_from_docstring_square_brackets(self) -> None:
        assert (
            infer_sig_from_docstring("fetch_row([maxrows, how]) -- Fetches stuff", "fetch_row")
            == []
        )

    def test_remove_misplaced_type_comments_1(self) -> None:
        good = """
        \u1234
        def f(x):  # type: (int) -> int

        def g(x):
            # type: (int) -> int

        def h():

            # type: () int

        x = 1  # type: int
        """

        assert_equal(remove_misplaced_type_comments(good), good)

    def test_remove_misplaced_type_comments_2(self) -> None:
        bad = """
        def f(x):
            # type: Callable[[int], int]
            pass

        #  type:  "foo"
        #  type:  'bar'
        x = 1
        # type: int
        """
        bad_fixed = """
        def f(x):

            pass



        x = 1

        """
        assert_equal(remove_misplaced_type_comments(bad), bad_fixed)

    def test_remove_misplaced_type_comments_3(self) -> None:
        bad = '''
        def f(x):
            """docstring"""
            # type: (int) -> int
            pass

        def g(x):
            """docstring
            """
            # type: (int) -> int
            pass
        '''
        bad_fixed = '''
        def f(x):
            """docstring"""

            pass

        def g(x):
            """docstring
            """

            pass
        '''
        assert_equal(remove_misplaced_type_comments(bad), bad_fixed)

    def test_remove_misplaced_type_comments_4(self) -> None:
        bad = """
        def f(x):
            '''docstring'''
            # type: (int) -> int
            pass

        def g(x):
            '''docstring
            '''
            # type: (int) -> int
            pass
        """
        bad_fixed = """
        def f(x):
            '''docstring'''

            pass

        def g(x):
            '''docstring
            '''

            pass
        """
        assert_equal(remove_misplaced_type_comments(bad), bad_fixed)

    def test_remove_misplaced_type_comments_5(self) -> None:
        bad = """
        def f(x):
            # type: (int, List[Any],
            #        float, bool) -> int
            pass

        def g(x):
            # type: (int, List[Any])
            pass
        """
        bad_fixed = """
        def f(x):

            #        float, bool) -> int
            pass

        def g(x):

            pass
        """
        assert_equal(remove_misplaced_type_comments(bad), bad_fixed)

    def test_remove_misplaced_type_comments_bytes(self) -> None:
        original = b"""
        \xbf
        def f(x):  # type: (int) -> int

        def g(x):
            # type: (int) -> int
            pass

        def h():
            # type: int
            pass

        x = 1  # type: int
        """

        dest = b"""
        \xbf
        def f(x):  # type: (int) -> int

        def g(x):
            # type: (int) -> int
            pass

        def h():

            pass

        x = 1  # type: int
        """

        assert_equal(remove_misplaced_type_comments(original), dest)

    @unittest.skipIf(sys.platform == "win32", "Tests building the paths common ancestor on *nix")
    def test_common_dir_prefix_unix(self) -> None:
        assert common_dir_prefix([]) == "."
        assert common_dir_prefix(["x.pyi"]) == "."
        assert common_dir_prefix(["./x.pyi"]) == "."
        assert common_dir_prefix(["foo/bar/x.pyi"]) == "foo/bar"
        assert common_dir_prefix(["foo/bar/x.pyi", "foo/bar/y.pyi"]) == "foo/bar"
        assert common_dir_prefix(["foo/bar/x.pyi", "foo/y.pyi"]) == "foo"
        assert common_dir_prefix(["foo/x.pyi", "foo/bar/y.pyi"]) == "foo"
        assert common_dir_prefix(["foo/bar/zar/x.pyi", "foo/y.pyi"]) == "foo"
        assert common_dir_prefix(["foo/x.pyi", "foo/bar/zar/y.pyi"]) == "foo"
        assert common_dir_prefix(["foo/bar/zar/x.pyi", "foo/bar/y.pyi"]) == "foo/bar"
        assert common_dir_prefix(["foo/bar/x.pyi", "foo/bar/zar/y.pyi"]) == "foo/bar"
        assert common_dir_prefix([r"foo/bar\x.pyi"]) == "foo"
        assert common_dir_prefix([r"foo\bar/x.pyi"]) == r"foo\bar"

    @unittest.skipIf(
        sys.platform != "win32", "Tests building the paths common ancestor on Windows"
    )
    def test_common_dir_prefix_win(self) -> None:
        assert common_dir_prefix(["x.pyi"]) == "."
        assert common_dir_prefix([r".\x.pyi"]) == "."
        assert common_dir_prefix([r"foo\bar\x.pyi"]) == r"foo\bar"
        assert common_dir_prefix([r"foo\bar\x.pyi", r"foo\bar\y.pyi"]) == r"foo\bar"
        assert common_dir_prefix([r"foo\bar\x.pyi", r"foo\y.pyi"]) == "foo"
        assert common_dir_prefix([r"foo\x.pyi", r"foo\bar\y.pyi"]) == "foo"
        assert common_dir_prefix([r"foo\bar\zar\x.pyi", r"foo\y.pyi"]) == "foo"
        assert common_dir_prefix([r"foo\x.pyi", r"foo\bar\zar\y.pyi"]) == "foo"
        assert common_dir_prefix([r"foo\bar\zar\x.pyi", r"foo\bar\y.pyi"]) == r"foo\bar"
        assert common_dir_prefix([r"foo\bar\x.pyi", r"foo\bar\zar\y.pyi"]) == r"foo\bar"
        assert common_dir_prefix([r"foo/bar\x.pyi"]) == r"foo\bar"
        assert common_dir_prefix([r"foo\bar/x.pyi"]) == r"foo\bar"
        assert common_dir_prefix([r"foo/bar/x.pyi"]) == r"foo\bar"

    def test_generate_inline_generic(self) -> None:
        assert generate_inline_generic(()) == ""
        T = TypeVar("T")
        assert generate_inline_generic((T,)) == "[T]"
        TBound = TypeVar("TBound", bound=int)
        assert generate_inline_generic((TBound,)) == "[TBound: int]"
        TBoundTuple = TypeVar("TBoundTuple", int, str)
        assert generate_inline_generic((TBoundTuple,)) == "[TBoundTuple: (int, str)]"
        P = ParamSpec("P")
        p_tuple = cast(Tuple[ParamSpec], (P,))
        assert generate_inline_generic(p_tuple) == "[**P]"
        U = TypeVarTuple("U")
        u_tuple = cast(Tuple[TypeVarTuple], (U,))
        assert generate_inline_generic(u_tuple) == "[*U]"
        all_tuple = cast(
            Tuple[TypeVar, TypeVar, TypeVar, ParamSpec, TypeVarTuple],
            (T, TBound, TBoundTuple, P, U),
        )
        assert (
            generate_inline_generic(all_tuple)
            == "[T, TBound: int, TBoundTuple: (int, str), **P, *U]"
        )


class StubgenHelpersSuite(unittest.TestCase):
    def test_is_blacklisted_path(self) -> None:
        assert not is_blacklisted_path("foo/bar.py")
        assert not is_blacklisted_path("foo.py")
        assert not is_blacklisted_path("foo/xvendor/bar.py")
        assert not is_blacklisted_path("foo/vendorx/bar.py")
        assert is_blacklisted_path("foo/vendor/bar.py")
        assert is_blacklisted_path("foo/vendored/bar.py")
        assert is_blacklisted_path("foo/vendored/bar/thing.py")
        assert is_blacklisted_path("foo/six.py")

    def test_is_non_library_module(self) -> None:
        assert not is_non_library_module("foo")
        assert not is_non_library_module("foo.bar")

        # The following could be test modules, but we are very conservative and
        # don't treat them as such since they could plausibly be real modules.
        assert not is_non_library_module("foo.bartest")
        assert not is_non_library_module("foo.bartests")
        assert not is_non_library_module("foo.testbar")

        assert is_non_library_module("foo.test")
        assert is_non_library_module("foo.test.foo")
        assert is_non_library_module("foo.tests")
        assert is_non_library_module("foo.tests.foo")
        assert is_non_library_module("foo.testing.foo")
        assert is_non_library_module("foo.SelfTest.foo")

        assert is_non_library_module("foo.test_bar")
        assert is_non_library_module("foo.bar_tests")
        assert is_non_library_module("foo.testing")
        assert is_non_library_module("foo.conftest")
        assert is_non_library_module("foo.bar_test_util")
        assert is_non_library_module("foo.bar_test_utils")
        assert is_non_library_module("foo.bar_test_base")

        assert is_non_library_module("foo.setup")

        assert is_non_library_module("foo.__main__")


class StubgenPythonSuite(DataSuite):
    """Data-driven end-to-end test cases that generate stub files.

    You can use these magic test case name suffixes:

    *_semanal
        Run semantic analysis (slow as this uses real stubs -- only use
        when necessary)
    *_import
        Import module and perform runtime introspection (in the current
        process!)

    You can use these magic comments:

    # flags: --some-stubgen-option ...
        Specify custom stubgen options

    # modules: module1 module2 ...
        Specify which modules to output (by default only 'main')
    """

    required_out_section = True
    base_path = "."
    files = ["stubgen.test"]

    @unittest.skipIf(sys.platform == "win32", "clean up fails on Windows")
    def run_case(self, testcase: DataDrivenTestCase) -> None:
        with local_sys_path_set():
            self.run_case_inner(testcase)

    def run_case_inner(self, testcase: DataDrivenTestCase) -> None:
        extra = []  # Extra command-line args
        mods = []  # Module names to process
        source = "\n".join(testcase.input)
        for file, content in testcase.files + [("./main.py", source)]:
            # Strip ./ prefix and .py suffix.
            mod = file[2:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod, _, _ = mod.rpartition(".")
            mods.append(mod)
            if "-p " not in source:
                extra.extend(["-m", mod])
            with open(file, "w") as f:
                f.write(content)

        options = self.parse_flags(source, extra)
        if sys.version_info < options.pyversion:
            pytest.skip()
        modules = self.parse_modules(source)
        out_dir = "out"
        try:
            try:
                if testcase.name.endswith("_inspect"):
                    options.inspect = True
                else:
                    if not testcase.name.endswith("_import"):
                        options.no_import = True
                    if not testcase.name.endswith("_semanal"):
                        options.parse_only = True

                generate_stubs(options)
                a: list[str] = []
                for module in modules:
                    fnam = module_to_path(out_dir, module)
                    self.add_file(fnam, a, header=len(modules) > 1)
            except CompileError as e:
                a = e.messages
            assert_string_arrays_equal(
                testcase.output, a, f"Invalid output ({testcase.file}, line {testcase.line})"
            )
        finally:
            for mod in mods:
                if mod in sys.modules:
                    del sys.modules[mod]
            shutil.rmtree(out_dir)

    def parse_flags(self, program_text: str, extra: list[str]) -> Options:
        flags = re.search("# flags: (.*)$", program_text, flags=re.MULTILINE)
        pyversion = None
        if flags:
            flag_list = flags.group(1).split()
            for i, flag in enumerate(flag_list):
                if flag.startswith("--python-version="):
                    pyversion = flag.split("=", 1)[1]
                    del flag_list[i]
                    break
        else:
            flag_list = []
        options = parse_options(flag_list + extra)
        if pyversion:
            # A hack to allow testing old python versions with new language constructs
            # This should be rarely used in general as stubgen output should not be version-specific
            major, minor = pyversion.split(".", 1)
            options.pyversion = (int(major), int(minor))
        if "--verbose" not in flag_list:
            options.quiet = True
        else:
            options.verbose = True
        return options

    def parse_modules(self, program_text: str) -> list[str]:
        modules = re.search("# modules: (.*)$", program_text, flags=re.MULTILINE)
        if modules:
            return modules.group(1).split()
        else:
            return ["main"]

    def add_file(self, path: str, result: list[str], header: bool) -> None:
        if not os.path.exists(path):
            result.append("<%s was not generated>" % path.replace("\\", "/"))
            return
        if header:
            result.append(f"# {path[4:]}")
        with open(path, encoding="utf8") as file:
            result.extend(file.read().splitlines())


self_arg = ArgSig(name="self")


class TestBaseClass:
    pass


class TestClass(TestBaseClass):
    pass


class StubgencSuite(unittest.TestCase):
    """Unit tests for stub generation from C modules using introspection.

    Note that these don't cover a lot!
    """

    def test_infer_hash_sig(self) -> None:
        assert_equal(infer_c_method_args("__hash__"), [self_arg])
        assert_equal(infer_method_ret_type("__hash__"), "int")

    def test_infer_getitem_sig(self) -> None:
        assert_equal(infer_c_method_args("__getitem__"), [self_arg, ArgSig(name="index")])

    def test_infer_setitem_sig(self) -> None:
        assert_equal(
            infer_c_method_args("__setitem__"),
            [self_arg, ArgSig(name="index"), ArgSig(name="object")],
        )
        assert_equal(infer_method_ret_type("__setitem__"), "None")

    def test_infer_eq_op_sig(self) -> None:
        for op in ("eq", "ne", "lt", "le", "gt", "ge"):
            assert_equal(
                infer_c_method_args(f"__{op}__"), [self_arg, ArgSig(name="other", type="object")]
            )

    def test_infer_binary_op_sig(self) -> None:
        for op in ("add", "radd", "sub", "rsub", "mul", "rmul"):
            assert_equal(infer_c_method_args(f"__{op}__"), [self_arg, ArgSig(name="other")])

    def test_infer_equality_op_sig(self) -> None:
        for op in ("eq", "ne", "lt", "le", "gt", "ge", "contains"):
            assert_equal(infer_method_ret_type(f"__{op}__"), "bool")

    def test_infer_unary_op_sig(self) -> None:
        for op in ("neg", "pos"):
            assert_equal(infer_c_method_args(f"__{op}__"), [self_arg])

    def test_infer_cast_sig(self) -> None:
        for op in ("float", "bool", "bytes", "int"):
            assert_equal(infer_method_ret_type(f"__{op}__"), op)

    def test_generate_class_stub_no_crash_for_object(self) -> None:
        output: list[str] = []
        mod = ModuleType("module", "")  # any module is fine
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)

        gen.generate_class_stub("alias", object, output)
        assert_equal(gen.get_imports().splitlines(), [])
        assert_equal(output[0], "class alias:")

    def test_generate_class_stub_variable_type_annotation(self) -> None:
        # This class mimics the stubgen unit test 'testClassVariable'
        class TestClassVariableCls:
            x = 1

        output: list[str] = []
        mod = ModuleType("module", "")  # any module is fine
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClassVariableCls, output)
        assert_equal(gen.get_imports().splitlines(), ["from typing import ClassVar"])
        assert_equal(output, ["class C:", "    x: ClassVar[int] = ..."])

    def test_non_c_generate_signature_with_kw_only_args(self) -> None:
        class TestClass:
            def test(
                self, arg0: str, *, keyword_only: str, keyword_only_with_default: int = 7
            ) -> None:
                pass

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.is_c_module = False
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(
                self_var="self",
                cls=TestClass,
                name="TestClass",
                docstring=getattr(TestClass, "__doc__", None),
            ),
        )
        assert_equal(
            output,
            [
                "def test(self, arg0: str, *, keyword_only: str, keyword_only_with_default: int = ...) -> None: ..."
            ],
        )

    def test_generate_c_type_inheritance(self) -> None:
        class TestClass(KeyError):
            pass

        output: list[str] = []
        mod = ModuleType("module, ")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClass, output)
        assert_equal(output, ["class C(KeyError): ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_inheritance_same_module(self) -> None:
        output: list[str] = []
        mod = ModuleType(TestBaseClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClass, output)
        assert_equal(output, ["class C(TestBaseClass): ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_inheritance_other_module(self) -> None:
        import argparse

        class TestClass(argparse.Action):
            pass

        output: list[str] = []
        mod = ModuleType("module", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClass, output)
        assert_equal(output, ["class C(argparse.Action): ..."])
        assert_equal(gen.get_imports().splitlines(), ["import argparse"])

    @unittest.skipIf(sys.version_info < (3, 12), "Inline Generics not supported before Python3.12")
    def test_inline_generic_class(self) -> None:
        T = TypeVar("T")

        class TestClass:
            __type_params__ = (T,)

        output: list[str] = []
        mod = ModuleType("module", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClass, output)
        assert_equal(output, ["class C[T]: ..."])

    @unittest.skipIf(sys.version_info < (3, 12), "Inline Generics not supported before Python3.12")
    def test_generic_class(self) -> None:
        exec("class Test[A]: ...")

        # type: ignore used for older versions of python type checking
        class TestClass[A]: ...  # type: ignore[invalid-syntax]

        output: list[str] = []
        mod = ModuleType("module", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClass, output)
        assert_equal(output, ["class C[A]: ..."])

    @unittest.skipIf(sys.version_info < (3, 12), "Inline Generics not supported before Python3.12")
    def test_inline_generic_function(self) -> None:

        if sys.version_info < (
            3,
            12,
        ):  # Done to prevent mypy [attr-defined] error on __type_params__ in older versions of python
            return

        T = TypeVar("T", bound=int)

        class TestClass:
            def test(self, arg0: T) -> T:
                """
                test(self, arg0: T) -> T
                """
                return arg0

            test.__type_params__ = (T,)

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test[T: int](self, arg0: T) -> T: ..."])

    def test_generate_c_type_inheritance_builtin_type(self) -> None:
        class TestClass(type):
            pass

        output: list[str] = []
        mod = ModuleType("module", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_class_stub("C", TestClass, output)
        assert_equal(output, ["class C(type): ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_with_docstring(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(self: TestClass, arg0: int)
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: int) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_with_docstring_no_self_arg(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(arg0: int)
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: int) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_classmethod(self) -> None:
        class TestClass:
            @classmethod
            def test(cls, arg0: str) -> None:
                pass

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="cls", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["@classmethod", "def test(cls, *args, **kwargs): ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_classmethod_with_overloads(self) -> None:
        class TestClass:
            @classmethod
            def test(self, arg0: str) -> None:
                """
                test(cls, arg0: str)
                test(cls, arg0: int)
                """
                pass

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="cls", cls=TestClass, name="TestClass"),
        )
        assert_equal(
            output,
            [
                "@overload",
                "@classmethod",
                "def test(cls, arg0: str) -> Any: ...",
                "@overload",
                "@classmethod",
                "def test(cls, arg0: int) -> Any: ...",
            ],
        )
        assert_equal(gen.get_imports().splitlines(), ["from typing import overload"])

    def test_generate_c_type_with_docstring_empty_default(self) -> None:
        class TestClass:
            def test(self, arg0: str = "") -> None:
                """
                test(self: TestClass, arg0: str = "")
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: str = ...) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_function_other_module_arg(self) -> None:
        """Test that if argument references type from other module, module will be imported."""

        # Provide different type in python spec than in docstring to make sure, that docstring
        # information is used.
        def test(arg0: str) -> None:
            """
            test(arg0: argparse.Action)
            """

        output: list[str] = []
        mod = ModuleType(self.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(arg0: argparse.Action) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), ["import argparse"])

    def test_generate_c_function_same_module(self) -> None:
        """Test that if annotation references type from same module but using full path, no module
        will be imported, and type specification will be striped to local reference.
        """

        # Provide different type in python spec than in docstring to make sure, that docstring
        # information is used.
        def test(arg0: str) -> None:
            """
            test(arg0: argparse.Action) -> argparse.Action
            """

        output: list[str] = []
        mod = ModuleType("argparse", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(arg0: Action) -> Action: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_function_other_module(self) -> None:
        """Test that if annotation references type from other module, module will be imported."""

        def test(arg0: str) -> None:
            """
            test(arg0: argparse.Action) -> argparse.Action
            """

        output: list[str] = []
        mod = ModuleType(self.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(arg0: argparse.Action) -> argparse.Action: ..."])
        assert_equal(gen.get_imports().splitlines(), ["import argparse"])

    def test_generate_c_function_same_module_nested(self) -> None:
        """Test that if annotation references type from same module but using full path, no module
        will be imported, and type specification will be stripped to local reference.
        """

        # Provide different type in python spec than in docstring to make sure, that docstring
        # information is used.
        def test(arg0: str) -> None:
            """
            test(arg0: list[argparse.Action]) -> list[argparse.Action]
            """

        output: list[str] = []
        mod = ModuleType("argparse", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(arg0: list[Action]) -> list[Action]: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_function_same_module_compound(self) -> None:
        """Test that if annotation references type from same module but using full path, no module
        will be imported, and type specification will be stripped to local reference.
        """

        # Provide different type in python spec than in docstring to make sure, that docstring
        # information is used.
        def test(arg0: str) -> None:
            """
            test(arg0: Union[argparse.Action, NoneType]) -> Tuple[argparse.Action, NoneType]
            """

        output: list[str] = []
        mod = ModuleType("argparse", "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(arg0: Union[Action, None]) -> Tuple[Action, None]: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_function_other_module_nested(self) -> None:
        """Test that if annotation references type from other module, module will be imported,
        and the import will be restricted to one of the known modules."""

        def test(arg0: str) -> None:
            """
            test(arg0: foo.bar.Action) -> other.Thing
            """

        output: list[str] = []
        mod = ModuleType(self.__module__, "")
        gen = InspectionStubGenerator(
            mod.__name__, known_modules=["foo", "foo.spangle", "bar"], module=mod
        )
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(arg0: foo.bar.Action) -> other.Thing: ..."])
        assert_equal(gen.get_imports().splitlines(), ["import foo", "import other"])

    def test_generate_c_function_no_crash_for_non_str_docstring(self) -> None:
        def test(arg0: str) -> None: ...

        test.__doc__ = property(lambda self: "test(arg0: str) -> None")  # type: ignore[assignment]

        output: list[str] = []
        mod = ModuleType(self.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub("test", test, output=output)
        assert_equal(output, ["def test(*args, **kwargs): ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_property_with_pybind11(self) -> None:
        """Signatures included by PyBind11 inside property.fget are read."""

        class TestClass:
            def get_attribute(self) -> None:
                """
                (self: TestClass) -> str
                """

            attribute = property(get_attribute, doc="")

        readwrite_properties: list[str] = []
        readonly_properties: list[str] = []
        mod = ModuleType("module", "")  # any module is fine
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_property_stub(
            "attribute",
            TestClass.__dict__["attribute"],
            TestClass.attribute,
            [],
            readwrite_properties,
            readonly_properties,
        )
        assert_equal(readwrite_properties, [])
        assert_equal(readonly_properties, ["@property", "def attribute(self) -> str: ..."])

    def test_generate_c_property_with_rw_property(self) -> None:
        class TestClass:
            def __init__(self) -> None:
                self._attribute = 0

            @property
            def attribute(self) -> int:
                return self._attribute

            @attribute.setter
            def attribute(self, value: int) -> None:
                self._attribute = value

        readwrite_properties: list[str] = []
        readonly_properties: list[str] = []
        mod = ModuleType("module", "")  # any module is fine
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_property_stub(
            "attribute",
            TestClass.__dict__["attribute"],
            TestClass.attribute,
            [],
            readwrite_properties,
            readonly_properties,
        )
        assert_equal(readwrite_properties, ["attribute: Incomplete"])
        assert_equal(readonly_properties, [])

    def test_generate_c_type_with_single_arg_generic(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(self: TestClass, arg0: List[int])
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: List[int]) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_with_double_arg_generic(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(self: TestClass, arg0: Dict[str, int])
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: Dict[str, int]) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_with_nested_generic(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(self: TestClass, arg0: Dict[str, List[int]])
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: Dict[str, List[int]]) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), [])

    def test_generate_c_type_with_generic_using_other_module_first(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(self: TestClass, arg0: Dict[argparse.Action, int])
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: Dict[argparse.Action, int]) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), ["import argparse"])

    def test_generate_c_type_with_generic_using_other_module_last(self) -> None:
        class TestClass:
            def test(self, arg0: str) -> None:
                """
                test(self: TestClass, arg0: Dict[str, argparse.Action])
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "test",
            TestClass.test,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(output, ["def test(self, arg0: Dict[str, argparse.Action]) -> Any: ..."])
        assert_equal(gen.get_imports().splitlines(), ["import argparse"])

    def test_generate_c_type_with_overload_pybind11(self) -> None:
        class TestClass:
            def __init__(self, arg0: str) -> None:
                """
                __init__(*args, **kwargs)
                Overloaded function.

                1. __init__(self: TestClass, arg0: str) -> None

                2. __init__(self: TestClass, arg0: str, arg1: str) -> None
                """

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "__init__",
            TestClass.__init__,
            output=output,
            class_info=ClassInfo(self_var="self", cls=TestClass, name="TestClass"),
        )
        assert_equal(
            output,
            [
                "@overload",
                "def __init__(self, arg0: str) -> None: ...",
                "@overload",
                "def __init__(self, arg0: str, arg1: str) -> None: ...",
                "@overload",
                "def __init__(self, *args, **kwargs) -> Any: ...",
            ],
        )
        assert_equal(gen.get_imports().splitlines(), ["from typing import overload"])

    def test_generate_c_type_with_overload_shiboken(self) -> None:
        class TestClass:
            """
            TestClass(self: TestClass, arg0: str) -> None
            TestClass(self: TestClass, arg0: str, arg1: str) -> None
            """

            def __init__(self, arg0: str) -> None:
                pass

        output: list[str] = []
        mod = ModuleType(TestClass.__module__, "")
        gen = InspectionStubGenerator(mod.__name__, known_modules=[mod.__name__], module=mod)
        gen.generate_function_stub(
            "__init__",
            TestClass.__init__,
            output=output,
            class_info=ClassInfo(
                self_var="self",
                cls=TestClass,
                name="TestClass",
                docstring=getattr(TestClass, "__doc__", None),
            ),
        )
        assert_equal(
            output,
            [
                "@overload",
                "def __init__(self, arg0: str) -> None: ...",
                "@overload",
                "def __init__(self, arg0: str, arg1: str) -> None: ...",
            ],
        )
        assert_equal(gen.get_imports().splitlines(), ["from typing import overload"])


class ArgSigSuite(unittest.TestCase):
    def test_repr(self) -> None:
        assert_equal(
            repr(ArgSig(name='asd"dsa')), "ArgSig(name='asd\"dsa', type=None, default=False)"
        )
        assert_equal(
            repr(ArgSig(name="asd'dsa")), 'ArgSig(name="asd\'dsa", type=None, default=False)'
        )
        assert_equal(repr(ArgSig("func", "str")), "ArgSig(name='func', type='str', default=False)")
        assert_equal(
            repr(ArgSig("func", "str", default=True)),
            "ArgSig(name='func', type='str', default=True)",
        )


class IsValidTypeSuite(unittest.TestCase):
    def test_is_valid_type(self) -> None:
        assert is_valid_type("int")
        assert is_valid_type("str")
        assert is_valid_type("Foo_Bar234")
        assert is_valid_type("foo.bar")
        assert is_valid_type("List[int]")
        assert is_valid_type("Dict[str, int]")
        assert is_valid_type("None")
        assert is_valid_type("Literal[26]")
        assert is_valid_type("Literal[0x1A]")
        assert is_valid_type('Literal["hello world"]')
        assert is_valid_type('Literal[b"hello world"]')
        assert is_valid_type('Literal[u"hello world"]')
        assert is_valid_type("Literal[True]")
        assert is_valid_type("Literal[Color.RED]")
        assert is_valid_type("Literal[None]")
        assert is_valid_type(
            'Literal[26, 0x1A, "hello world", b"hello world", u"hello world", True, Color.RED, None]'
        )
        assert not is_valid_type("foo-bar")
        assert not is_valid_type("x->y")
        assert not is_valid_type("True")
        assert not is_valid_type("False")
        assert not is_valid_type("x,y")
        assert not is_valid_type("x, y")


class ModuleInspectSuite(unittest.TestCase):
    def test_python_module(self) -> None:
        with ModuleInspect() as m:
            p = m.get_package_properties("inspect")
            assert p is not None
            assert p.name == "inspect"
            assert p.file
            assert p.path is None
            assert p.is_c_module is False
            assert p.subpackages == []

    def test_python_package(self) -> None:
        with ModuleInspect() as m:
            p = m.get_package_properties("unittest")
            assert p is not None
            assert p.name == "unittest"
            assert p.file
            assert p.path
            assert p.is_c_module is False
            assert p.subpackages
            assert all(sub.startswith("unittest.") for sub in p.subpackages)

    def test_c_module(self) -> None:
        with ModuleInspect() as m:
            p = m.get_package_properties("_socket")
            assert p is not None
            assert p.name == "_socket"
            assert p.path is None
            assert p.is_c_module is True
            assert p.subpackages == []

    def test_non_existent(self) -> None:
        with ModuleInspect() as m:
            with self.assertRaises(InspectError) as e:
                m.get_package_properties("foobar-non-existent")
            assert str(e.exception) == "No module named 'foobar-non-existent'"


def module_to_path(out_dir: str, module: str) -> str:
    fnam = os.path.join(out_dir, f"{module.replace('.', '/')}.pyi")
    if not os.path.exists(fnam):
        alt_fnam = fnam.replace(".pyi", "/__init__.pyi")
        if os.path.exists(alt_fnam):
            return alt_fnam
    return fnam
