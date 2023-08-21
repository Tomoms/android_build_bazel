# Copyright (C) 2022 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import io
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Final, Optional

import clone
import finder
import cuj
import finder
import ui
import util
import random
import re
from cuj import CujGroup
from cuj import CujStep
from cuj import InWorkspace
from cuj import Verifier
from cuj import de_src
from cuj import src
from util import BuildType

"""
Provides some representative CUJs. If you wanted to manually run something but
would like the metrics to be collated in the metrics.csv file, use
`perf_metrics.py` as a stand-alone after your build.
"""

Warmup: Final[CujGroup] = CujGroup("WARMUP", [CujStep("no change", lambda: None)])


def modify_revert(file: Path, text: Optional[str] = None) -> CujGroup:
    """
    :param file: the file to be modified and reverted
    :param text: the text to be appended to the file to modify it
    :return: A pair of CujSteps, where the first modifies the file and the
    second reverts the modification
    """
    if text is None:
        text = f"//BOGUS {uuid.uuid4()}\n"
    if not file.exists():
        raise RuntimeError(f"{file} does not exist")

    def add_line():
        with open(file, mode="a") as f:
            f.write(text)

    def revert():
        with open(file, mode="rb+") as f:
            # assume UTF-8
            f.seek(-len(text), io.SEEK_END)
            f.truncate()

    return CujGroup(
        de_src(file), [CujStep("modify", add_line), CujStep("revert", revert)]
    )


def regex_modify_revert(file: Path, pattern: str, replacement: str, modify_type: str) -> CujGroup:
    """
    :param file: the file to be edited and reverted
    :param pattern: the strings that will be replaced
    :param replacement: the replaced strings
    :param modify_type: types of modification
    :return: A pair of CujSteps, where the fist modifies the file and the
    second reverts it
    """
    if not file.exists():
        raise RuntimeError(f"{file} does not exist")

    original_text: str

    def modify():
        nonlocal original_text
        original_text = file.read_text()
        modified_text = re.sub(pattern, replacement, original_text, count=1, flags=re.MULTILINE)
        file.write_text(modified_text)

    def revert():
        file.write_text(original_text)

    return CujGroup(
        de_src(file), [CujStep(modify_type, modify), CujStep("revert", revert)]
    )


def modify_private_method(file: Path) -> CujGroup:
    pattern = r'(private static boolean.*{)'
    replacement =  r'\1 Log.d("Placeholder", "Placeholder{}");'.format(random.randint(0,1000))
    modify_type = "modify_private_method"
    return regex_modify_revert(file, pattern, replacement, modify_type)


def add_private_field(file: Path) -> CujGroup:
    pattern = r'^\}$'
    replacement =  r'private static final int FOO = ' + str(random.randint(0,1000)) + ';\n}'
    modify_type = "add_private_field"
    return regex_modify_revert(file, pattern, replacement, modify_type)


def add_public_api(file: Path) -> CujGroup:
    pattern = r'\}$'
    replacement =  r'public static final int BAZ = ' + str(random.randint(0,1000)) + ';\n}'
    modify_type = "add_public_api"
    return regex_modify_revert(file, pattern, replacement, modify_type)


def modify_resource(file: Path) -> CujGroup:
    pattern = r'>0<'
    replacement = r'>' + str(random.randint(0,1000)) + r'<'
    modify_type = "modify_resource"
    return regex_modify_revert(file, pattern, replacement, modify_type)


def add_resource(file: Path) -> CujGroup:
    pattern = r'</resources>'
    replacement = r'    <integer name="foo">' + str(random.randint(0,1000)) + r'</integer>\n</resources>'
    modify_type = "add_resource"
    return regex_modify_revert(file, pattern, replacement, modify_type)


def create_delete(file: Path, ws: InWorkspace, text: Optional[str] = None) -> CujGroup:
    """
    :param file: the file to be created and deleted
    :param ws: the expectation for the counterpart file in symlink
    forest (aka the synthetic bazel workspace) when its created
    :param text: the content of the file
    :return: A pair of CujSteps, where the fist creates the file and the
    second deletes it
    """
    if text is None:
        text = f"//Test File: safe to delete {uuid.uuid4()}\n"
    missing_dirs = [f for f in file.parents if not f.exists()]
    shallowest_missing_dir = missing_dirs[-1] if len(missing_dirs) else None

    def create():
        if file.exists():
            raise RuntimeError(
                f"File {file} already exists. Interrupted an earlier run?\n"
                "TIP: `repo status` and revert changes!!!"
            )
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch(exist_ok=False)
        with open(file, mode="w") as f:
            f.write(text)

    def delete():
        if shallowest_missing_dir:
            shutil.rmtree(shallowest_missing_dir)
        else:
            file.unlink(missing_ok=False)

    return CujGroup(
        de_src(file),
        [
            CujStep("create", create, ws.verifier(file)),
            CujStep("delete", delete, InWorkspace.OMISSION.verifier(file)),
        ],
    )


def create_delete_bp(bp_file: Path) -> CujGroup:
    """
    This is basically the same as "create_delete" but with canned content for
    an Android.bp file.
    """
    return create_delete(
        bp_file,
        InWorkspace.SYMLINK,
        'filegroup { name: "test-bogus-filegroup", srcs: ["**/*.md"] }',
    )


def delete_restore(original: Path, ws: InWorkspace) -> CujGroup:
    """
    :param original: The file to be deleted then restored
    :param ws: When restored, expectation for the file's counterpart in the
    symlink forest (aka synthetic bazel workspace)
    :return: A pair of CujSteps, where the first deletes a file and the second
    restores it
    """
    tempdir = Path(tempfile.gettempdir())
    if tempdir.is_relative_to(util.get_top_dir()):
        raise SystemExit(f"Temp dir {tempdir} is under source tree")
    if tempdir.is_relative_to(util.get_out_dir()):
        raise SystemExit(
            f"Temp dir {tempdir} is under " f"OUT dir {util.get_out_dir()}"
        )
    copied = tempdir.joinpath(f"{original.name}-{uuid.uuid4()}.bak")

    def move_to_tempdir_to_mimic_deletion():
        logging.warning("MOVING %s TO %s", de_src(original), copied)
        original.rename(copied)

    return CujGroup(
        de_src(original),
        [
            CujStep(
                "delete",
                move_to_tempdir_to_mimic_deletion,
                InWorkspace.OMISSION.verifier(original),
            ),
            CujStep("restore", lambda: copied.rename(original), ws.verifier(original)),
        ],
    )


def replace_link_with_dir(p: Path):
    """Create a file, replace it with a non-empty directory, delete it"""
    cd = create_delete(p, InWorkspace.SYMLINK)
    create_file: CujStep
    delete_file: CujStep
    create_file, delete_file, *tail = cd.steps
    assert len(tail) == 0

    # an Android.bp is always a symlink in the workspace and thus its parent
    # will be a directory in the workspace
    create_dir: CujStep
    delete_dir: CujStep
    create_dir, delete_dir, *tail = create_delete_bp(p.joinpath("Android.bp")).steps
    assert len(tail) == 0

    def replace_it():
        delete_file.apply_change()
        create_dir.apply_change()

    return CujGroup(
        cd.description,
        [
            create_file,
            CujStep(
                f"{de_src(p)}/Android.bp instead of", replace_it, create_dir.verify
            ),
            delete_dir,
        ],
    )


def content_verfiers(ws_build_file: Path, content: str) -> tuple[Verifier, Verifier]:
    def search() -> bool:
        with open(ws_build_file, "r") as f:
            for line in f:
                if line == content:
                    return True
        return False

    @cuj.skip_for(BuildType.SOONG_ONLY)
    def contains():
        if not search():
            raise AssertionError(
                f"{de_src(ws_build_file)} expected to contain {content}"
            )
        logging.info(f"VERIFIED {de_src(ws_build_file)} contains {content}")

    @cuj.skip_for(BuildType.SOONG_ONLY)
    def does_not_contain():
        if search():
            raise AssertionError(
                f"{de_src(ws_build_file)} not expected to contain {content}"
            )
        logging.info(f"VERIFIED {de_src(ws_build_file)} does not contain {content}")

    return contains, does_not_contain


def modify_revert_kept_build_file(build_file: Path) -> CujGroup:
    content = f"//BOGUS {uuid.uuid4()}\n"
    step1, step2, *tail = modify_revert(build_file, content).steps
    assert len(tail) == 0
    ws_build_file = InWorkspace.ws_counterpart(build_file).with_name("BUILD.bazel")
    merge_prover, merge_disprover = content_verfiers(ws_build_file, content)
    return CujGroup(
        de_src(build_file),
        [
            CujStep(
                step1.verb, step1.apply_change, cuj.sequence(step1.verify, merge_prover)
            ),
            CujStep(
                step2.verb,
                step2.apply_change,
                cuj.sequence(step2.verify, merge_disprover),
            ),
        ],
    )


def create_delete_kept_build_file(build_file: Path) -> CujGroup:
    content = f"//BOGUS {uuid.uuid4()}\n"
    ws_build_file = InWorkspace.ws_counterpart(build_file).with_name("BUILD.bazel")
    if build_file.name == "BUILD.bazel":
        ws = InWorkspace.NOT_UNDER_SYMLINK
    elif build_file.name == "BUILD":
        ws = InWorkspace.SYMLINK
    else:
        raise RuntimeError(f"Illegal name for a build file {build_file}")

    merge_prover, merge_disprover = content_verfiers(ws_build_file, content)

    step1: CujStep
    step2: CujStep
    step1, step2, *tail = create_delete(build_file, ws, content).steps
    assert len(tail) == 0
    return CujGroup(
        de_src(build_file),
        [
            CujStep(
                step1.verb, step1.apply_change, cuj.sequence(step1.verify, merge_prover)
            ),
            CujStep(
                step2.verb,
                step2.apply_change,
                cuj.sequence(step2.verify, merge_disprover),
            ),
        ],
    )


def create_delete_unkept_build_file(build_file) -> CujGroup:
    content = f"//BOGUS {uuid.uuid4()}\n"
    ws_build_file = InWorkspace.ws_counterpart(build_file).with_name("BUILD.bazel")
    step1: CujStep
    step2: CujStep
    step1, step2, *tail = create_delete(build_file, InWorkspace.SYMLINK, content).steps
    assert len(tail) == 0
    _, merge_disprover = content_verfiers(ws_build_file, content)
    return CujGroup(
        de_src(build_file),
        [
            CujStep(
                step1.verb,
                step1.apply_change,
                cuj.sequence(step1.verify, merge_disprover),
            ),
            CujStep(
                step2.verb,
                step2.apply_change,
                cuj.sequence(step2.verify, merge_disprover),
            ),
        ],
    )


def _kept_build_cujs() -> list[CujGroup]:
    # Bp2BuildKeepExistingBuildFile(build/bazel) is True(recursive)
    kept = src("build/bazel")
    finder.confirm(
        kept,
        "compliance/Android.bp",
        "!compliance/BUILD",
        "!compliance/BUILD.bazel",
        "rules/python/BUILD",
    )

    return [
        *[
            create_delete_kept_build_file(kept.joinpath("compliance").joinpath(b))
            for b in ["BUILD", "BUILD.bazel"]
        ],
        create_delete(kept.joinpath("BUILD/kept-dir"), InWorkspace.SYMLINK),
        modify_revert_kept_build_file(kept.joinpath("rules/python/BUILD")),
    ]


def _unkept_build_cujs() -> list[CujGroup]:
    # Bp2BuildKeepExistingBuildFile(bionic) is False(recursive)
    unkept = src("bionic/libm")
    finder.confirm(unkept, "Android.bp", "!BUILD", "!BUILD.bazel")
    return [
        *[
            create_delete_unkept_build_file(unkept.joinpath(b))
            for b in ["BUILD", "BUILD.bazel"]
        ],
        *[
            create_delete(build_file, InWorkspace.OMISSION)
            for build_file in [
                unkept.joinpath("bogus-unkept/BUILD"),
                unkept.joinpath("bogus-unkept/BUILD.bazel"),
            ]
        ],
        create_delete(unkept.joinpath("BUILD/unkept-dir"), InWorkspace.SYMLINK),
    ]


@functools.cache
def get_cujgroups() -> list[CujGroup]:
    # we are choosing "package" directories that have Android.bp but
    # not BUILD nor BUILD.bazel because
    # we can't tell if ShouldKeepExistingBuildFile would be True or not
    non_empty_dir = "*/*"
    pkg = src("art")
    finder.confirm(pkg, non_empty_dir, "Android.bp", "!BUILD*")
    pkg_free = src("bionic/docs")
    finder.confirm(pkg_free, non_empty_dir, "!**/Android.bp", "!**/BUILD*")
    ancestor = src("bionic")
    finder.confirm(ancestor, "**/Android.bp", "!Android.bp", "!BUILD*")
    leaf_pkg_free = src("bionic/build")
    finder.confirm(leaf_pkg_free, f"!{non_empty_dir}", "!**/Android.bp", "!**/BUILD*")

    android_bp_cujs = [
        modify_revert(src("Android.bp")),
        *[
            create_delete_bp(d.joinpath("Android.bp"))
            for d in [ancestor, pkg_free, leaf_pkg_free]
        ],
    ]
    mixed_build_launch_cujs = [
        modify_revert(src("bionic/libc/tzcode/asctime.c")),
        modify_revert(src("bionic/libc/stdio/stdio.cpp")),
        modify_revert(src("packages/modules/adb/daemon/main.cpp")),
        modify_revert(src("frameworks/base/core/java/android/view/View.java")),
        modify_revert(src("frameworks/base/core/java/android/provider/Settings.java")),
        modify_private_method(src("frameworks/base/core/java/android/provider/Settings.java")),
        add_private_field(src("frameworks/base/core/java/android/provider/Settings.java")),
        add_public_api(src("frameworks/base/core/java/android/provider/Settings.java")),
        modify_private_method(src("frameworks/base/services/core/java/com/android/server/am/ActivityManagerService.java")),
        add_private_field(src("frameworks/base/services/core/java/com/android/server/am/ActivityManagerService.java")),
        add_public_api(src("frameworks/base/services/core/java/com/android/server/am/ActivityManagerService.java")),
        modify_resource(src("frameworks/base/core/res/res/values/config.xml")),
        add_resource(src("frameworks/base/core/res/res/values/config.xml")),
    ]
    unreferenced_file_cujs = [
        *[
            create_delete(d.joinpath("unreferenced.txt"), InWorkspace.SYMLINK)
            for d in [ancestor, pkg]
        ],
        *[
            create_delete(d.joinpath("unreferenced.txt"), InWorkspace.UNDER_SYMLINK)
            for d in [pkg_free, leaf_pkg_free]
        ],
    ]

    cc_ = (
        lambda t, name: t.startswith("cc_")
        and "test" not in t
        and not name.startswith("libcrypto")  # has some unique hash
    )
    libNN = lambda t, name: t == "cc_library_shared" and name == "libneuralnetworks"
    cloning_cujs = [
        clone.get_cuj_group({src("."): clone.type_in("genrule")}, "genrules"),
        clone.get_cuj_group({src("."): cc_}, "cc_"),
        clone.get_cuj_group(
            {src("packages/modules/adb/Android.bp"): clone.name_in("adbd")}, "adbd"
        ),
        clone.get_cuj_group(
            {src("packages/modules/NeuralNetworks/runtime/Android.bp"): libNN}, "libNN"
        ),
        clone.get_cuj_group(
            {
                src("packages/modules/adb/Android.bp"): clone.name_in("adbd"),
                src("packages/modules/NeuralNetworks/runtime/Android.bp"): libNN,
            },
            "adbd&libNN",
        ),
    ]

    def clean():
        if ui.get_user_input().log_dir.is_relative_to(util.get_top_dir()):
            raise AssertionError(
                f"specify a different LOG_DIR: {ui.get_user_input().log_dir}"
            )
        if util.get_out_dir().exists():
            shutil.rmtree(util.get_out_dir())

    return [
        CujGroup("", [CujStep("clean", clean)]),
        CujGroup("", Warmup.steps),  # to allow a "no change" CUJ option
        *cloning_cujs,
        create_delete(src("bionic/libc/tzcode/globbed.c"), InWorkspace.UNDER_SYMLINK),
        # TODO (usta): find targets that should be affected
        *[
            delete_restore(f, InWorkspace.SYMLINK)
            for f in [
                src("bionic/libc/version_script.txt"),
                src("external/cbor-java/AndroidManifest.xml"),
            ]
        ],
        *unreferenced_file_cujs,
        *mixed_build_launch_cujs,
        *android_bp_cujs,
        *_unkept_build_cujs(),
        *_kept_build_cujs(),
        replace_link_with_dir(pkg.joinpath("bogus.txt")),
        # TODO(usta): add a dangling symlink
    ]
