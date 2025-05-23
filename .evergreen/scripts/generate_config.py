# Note: See CONTRIBUTING.md for how to update/run this file.
from __future__ import annotations

import sys
from itertools import product

from generate_config_utils import (
    ALL_PYTHONS,
    ALL_VERSIONS,
    AUTH_SSLS,
    BATCHTIME_WEEK,
    C_EXTS,
    CPYTHONS,
    DEFAULT_HOST,
    HOSTS,
    MIN_MAX_PYTHON,
    OTHER_HOSTS,
    PYPYS,
    SUB_TASKS,
    SYNCS,
    TOPOLOGIES,
    create_variant,
    get_assume_role,
    get_s3_put,
    get_subprocess_exec,
    get_task_name,
    get_variant_name,
    get_versions_from,
    get_versions_until,
    handle_c_ext,
    write_functions_to_file,
    write_tasks_to_file,
    write_variants_to_file,
    zip_cycle,
)
from shrub.v3.evg_build_variant import BuildVariant
from shrub.v3.evg_command import (
    FunctionCall,
    archive_targz_pack,
    attach_results,
    attach_xunit_results,
    ec2_assume_role,
    expansions_update,
    git_get_project,
    perf_send,
)
from shrub.v3.evg_task import EvgTask, EvgTaskDependency, EvgTaskRef

##############
# Variants
##############


def create_ocsp_variants() -> list[BuildVariant]:
    variants = []
    # OCSP tests on default host with all servers v4.4+.
    # MongoDB servers on Windows and MacOS do not staple OCSP responses and only support RSA.
    # Only test with MongoDB 4.4 and latest.
    for host_name in ["rhel8", "win64", "macos"]:
        host = HOSTS[host_name]
        if host == DEFAULT_HOST:
            tasks = [".ocsp"]
        else:
            tasks = [".ocsp-rsa !.ocsp-staple .latest", ".ocsp-rsa !.ocsp-staple .4.4"]
        variant = create_variant(
            tasks,
            get_variant_name("OCSP", host),
            host=host,
            batchtime=BATCHTIME_WEEK,
        )
        variants.append(variant)
    return variants


def create_server_version_variants() -> list[BuildVariant]:
    variants = []
    for version in ALL_VERSIONS:
        display_name = get_variant_name("* MongoDB", version=version)
        variant = create_variant(
            [".server-version"], display_name, host=DEFAULT_HOST, tags=["coverage_tag"]
        )
        variants.append(variant)
    return variants


def create_standard_nonlinux_variants() -> list[BuildVariant]:
    variants = []
    base_display_name = "* Test"

    # Test a subset on each of the other platforms.
    for host_name in ("macos", "macos-arm64", "win64", "win32"):
        tasks = [".standard-non-linux"]
        # MacOS arm64 only works on server versions 6.0+
        if host_name == "macos-arm64":
            tasks = [
                f".standard-non-linux .server-{version}" for version in get_versions_from("6.0")
            ]
        host = HOSTS[host_name]
        tags = ["standard-non-linux"]
        expansions = dict()
        if host_name == "win32":
            expansions["IS_WIN32"] = "1"
        display_name = get_variant_name(base_display_name, host)
        variant = create_variant(tasks, display_name, host=host, tags=tags, expansions=expansions)
        variants.append(variant)

    return variants


def create_free_threaded_variants() -> list[BuildVariant]:
    variants = []
    for host_name in ("rhel8", "macos", "macos-arm64", "win64"):
        if host_name == "win64":
            # TODO: PYTHON-5027
            continue
        tasks = [".free-threading"]
        host = HOSTS[host_name]
        python = "3.13t"
        display_name = get_variant_name("Free-threaded", host, python=python)
        variant = create_variant(tasks, display_name, python=python, host=host)
        variants.append(variant)
    return variants


def create_encryption_variants() -> list[BuildVariant]:
    variants = []
    tags = ["encryption_tag"]
    batchtime = BATCHTIME_WEEK

    def get_encryption_expansions(encryption):
        expansions = dict(TEST_NAME="encryption")
        if "crypt_shared" in encryption:
            expansions["TEST_CRYPT_SHARED"] = "true"
        if "PyOpenSSL" in encryption:
            expansions["SUB_TEST_NAME"] = "pyopenssl"
        return expansions

    host = DEFAULT_HOST

    # Test against all server versions for the three main python versions.
    encryptions = ["Encryption", "Encryption crypt_shared"]
    for encryption, python in product(encryptions, [*MIN_MAX_PYTHON, PYPYS[-1]]):
        expansions = get_encryption_expansions(encryption)
        display_name = get_variant_name(encryption, host, python=python, **expansions)
        variant = create_variant(
            [f"{t} .sync_async" for t in SUB_TASKS],
            display_name,
            python=python,
            host=host,
            expansions=expansions,
            batchtime=batchtime,
            tags=tags,
        )
        variants.append(variant)

    # Test PyOpenSSL against on all server versions for all python versions.
    for encryption, python in product(["Encryption PyOpenSSL"], [*MIN_MAX_PYTHON, PYPYS[-1]]):
        expansions = get_encryption_expansions(encryption)
        display_name = get_variant_name(encryption, host, python=python, **expansions)
        variant = create_variant(
            [f"{t} .sync" for t in SUB_TASKS],
            display_name,
            python=python,
            host=host,
            expansions=expansions,
            batchtime=batchtime,
            tags=tags,
        )
        variants.append(variant)

    # Test the rest of the pythons on linux for all server versions.
    for encryption, python, task in zip_cycle(encryptions, CPYTHONS[1:-1] + PYPYS[:-1], SUB_TASKS):
        expansions = get_encryption_expansions(encryption)
        display_name = get_variant_name(encryption, host, python=python, **expansions)
        variant = create_variant(
            [f"{task} .sync_async"],
            display_name,
            python=python,
            host=host,
            expansions=expansions,
        )
        variants.append(variant)

    # Test on macos and linux on one server version and topology for min and max python.
    encryptions = ["Encryption", "Encryption crypt_shared"]
    task_names = [".latest .replica_set .sync_async"]
    for host_name, encryption, python in product(["macos", "win64"], encryptions, MIN_MAX_PYTHON):
        host = HOSTS[host_name]
        expansions = get_encryption_expansions(encryption)
        display_name = get_variant_name(encryption, host, python=python, **expansions)
        variant = create_variant(
            task_names,
            display_name,
            python=python,
            host=host,
            expansions=expansions,
            batchtime=batchtime,
            tags=tags,
        )
        variants.append(variant)
    return variants


def create_load_balancer_variants():
    # Load balancer tests - run all supported server versions using the lowest supported python.
    return [
        create_variant(
            [".load-balancer"], "Load Balancer", host=DEFAULT_HOST, batchtime=BATCHTIME_WEEK
        )
    ]


def create_compression_variants():
    # Compression tests - use the standard linux tests.
    host = DEFAULT_HOST
    variants = []
    for compressor in "snappy", "zlib", "zstd":
        expansions = dict(COMPRESSOR=compressor)
        if compressor == "zstd":
            tasks = [".standard-linux !.server-4.0"]
        else:
            tasks = [".standard-linux"]
        display_name = get_variant_name(f"Compression {compressor}", host)
        variants.append(
            create_variant(
                tasks,
                display_name,
                host=host,
                expansions=expansions,
            )
        )
    return variants


def create_enterprise_auth_variants():
    variants = []
    for host in [HOSTS["macos"], HOSTS["win64"], DEFAULT_HOST]:
        display_name = get_variant_name("Auth Enterprise", host)
        if host == DEFAULT_HOST:
            tags = [".enterprise_auth"]
        else:
            tags = [".enterprise_auth !.pypy"]
        variant = create_variant(tags, display_name, host=host)
        variants.append(variant)

    return variants


def create_pyopenssl_variants():
    base_name = "PyOpenSSL"
    batchtime = BATCHTIME_WEEK
    expansions = dict(TEST_NAME="pyopenssl")
    variants = []

    for python in ALL_PYTHONS:
        # Only test "noauth" with min python.
        auth = "noauth" if python == CPYTHONS[0] else "auth"
        ssl = "nossl" if auth == "noauth" else "ssl"
        if python == CPYTHONS[0]:
            host = HOSTS["macos"]
        elif python == CPYTHONS[-1]:
            host = HOSTS["win64"]
        else:
            host = DEFAULT_HOST

        display_name = get_variant_name(base_name, host, python=python)
        variant = create_variant(
            [f".replica_set .{auth} .{ssl} .sync", f".7.0 .{auth} .{ssl} .sync"],
            display_name,
            python=python,
            host=host,
            expansions=expansions,
            batchtime=batchtime,
        )
        variants.append(variant)

    return variants


def create_storage_engine_variants():
    host = DEFAULT_HOST
    engines = ["InMemory", "MMAPv1"]
    variants = []
    for engine in engines:
        python = CPYTHONS[0]
        expansions = dict(STORAGE_ENGINE=engine.lower())
        if engine == engines[0]:
            tasks = [f".standalone .noauth .nossl .{v} .sync_async" for v in ALL_VERSIONS]
        else:
            # MongoDB 4.2 drops support for MMAPv1
            versions = get_versions_until("4.0")
            tasks = [f".standalone .{v} .noauth .nossl .sync_async" for v in versions] + [
                f".replica_set .{v} .noauth .nossl .sync_async" for v in versions
            ]
        display_name = get_variant_name(f"Storage {engine}", host, python=python)
        variant = create_variant(
            tasks, display_name, host=host, python=python, expansions=expansions
        )
        variants.append(variant)
    return variants


def create_stable_api_variants():
    host = DEFAULT_HOST
    tags = ["versionedApi_tag"]
    variants = []
    types = ["require v1", "accept v2"]

    # All python versions across platforms.
    for python, test_type in product(MIN_MAX_PYTHON, types):
        expansions = dict(AUTH="auth")
        # Test against a cluster with requireApiVersion=1.
        if test_type == types[0]:
            # REQUIRE_API_VERSION is set to make drivers-evergreen-tools
            # start a cluster with the requireApiVersion parameter.
            expansions["REQUIRE_API_VERSION"] = "1"
            # MONGODB_API_VERSION is the apiVersion to use in the test suite.
            expansions["MONGODB_API_VERSION"] = "1"
            tasks = [
                f"!.replica_set .{v} .noauth .nossl .sync_async" for v in get_versions_from("5.0")
            ]
        else:
            # Test against a cluster with acceptApiVersion2 but without
            # requireApiVersion, and don't automatically add apiVersion to
            # clients created in the test suite.
            expansions["ORCHESTRATION_FILE"] = "versioned-api-testing.json"
            tasks = [
                f".standalone .{v} .noauth .nossl .sync_async" for v in get_versions_from("5.0")
            ]
        base_display_name = f"Stable API {test_type}"
        display_name = get_variant_name(base_display_name, host, python=python, **expansions)
        variant = create_variant(
            tasks, display_name, host=host, python=python, tags=tags, expansions=expansions
        )
        variants.append(variant)

    return variants


def create_green_framework_variants():
    variants = []
    tasks = [".standalone .noauth .nossl .sync_async"]
    host = DEFAULT_HOST
    for python, framework in product([CPYTHONS[0], CPYTHONS[-1]], ["eventlet", "gevent"]):
        expansions = dict(GREEN_FRAMEWORK=framework, AUTH="auth", SSL="ssl")
        display_name = get_variant_name(f"Green {framework.capitalize()}", host, python=python)
        variant = create_variant(
            tasks, display_name, host=host, python=python, expansions=expansions
        )
        variants.append(variant)
    return variants


def create_no_c_ext_variants():
    variants = []
    host = DEFAULT_HOST
    for python, topology in zip_cycle(CPYTHONS, TOPOLOGIES):
        tasks = [f".{topology} .noauth .nossl !.sync_async"]
        expansions = dict()
        handle_c_ext(C_EXTS[0], expansions)
        display_name = get_variant_name("No C Ext", host, python=python)
        variant = create_variant(
            tasks, display_name, host=host, python=python, expansions=expansions
        )
        variants.append(variant)
    return variants


def create_atlas_data_lake_variants():
    variants = []
    host = HOSTS["ubuntu22"]
    for python in MIN_MAX_PYTHON:
        tasks = [".atlas_data_lake"]
        display_name = get_variant_name("Atlas Data Lake", host, python=python)
        variant = create_variant(tasks, display_name, host=host, python=python)
        variants.append(variant)
    return variants


def create_mod_wsgi_variants():
    variants = []
    host = HOSTS["ubuntu22"]
    tasks = [".mod_wsgi"]
    expansions = dict(MOD_WSGI_VERSION="4")
    for python in MIN_MAX_PYTHON:
        display_name = get_variant_name("mod_wsgi", host, python=python)
        variant = create_variant(
            tasks, display_name, host=host, python=python, expansions=expansions
        )
        variants.append(variant)
    return variants


def create_disable_test_commands_variants():
    host = DEFAULT_HOST
    expansions = dict(AUTH="auth", SSL="ssl", DISABLE_TEST_COMMANDS="1")
    python = CPYTHONS[0]
    display_name = get_variant_name("Disable test commands", host, python=python)
    tasks = [".latest .sync_async"]
    return [create_variant(tasks, display_name, host=host, python=python, expansions=expansions)]


def create_serverless_variants():
    host = DEFAULT_HOST
    batchtime = BATCHTIME_WEEK
    tasks = [".serverless"]
    base_name = "Serverless"
    return [
        create_variant(
            tasks,
            get_variant_name(base_name, host, python=python),
            host=host,
            python=python,
            batchtime=batchtime,
        )
        for python in MIN_MAX_PYTHON
    ]


def create_oidc_auth_variants():
    variants = []
    for host_name in ["ubuntu22", "macos", "win64"]:
        if host_name == "ubuntu22":
            tasks = [".auth_oidc"]
        else:
            tasks = [".auth_oidc !.auth_oidc_remote"]
        host = HOSTS[host_name]
        variants.append(
            create_variant(
                tasks,
                get_variant_name("Auth OIDC", host),
                host=host,
                batchtime=BATCHTIME_WEEK,
            )
        )
    return variants


def create_search_index_variants():
    host = DEFAULT_HOST
    python = CPYTHONS[0]
    return [
        create_variant(
            [".search_index"],
            get_variant_name("Search Index Helpers", host, python=python),
            python=python,
            host=host,
        )
    ]


def create_mockupdb_variants():
    host = DEFAULT_HOST
    python = CPYTHONS[0]
    return [
        create_variant(
            [".mockupdb"],
            get_variant_name("MockupDB", host, python=python),
            python=python,
            host=host,
        )
    ]


def create_doctests_variants():
    host = DEFAULT_HOST
    python = CPYTHONS[0]
    return [
        create_variant(
            [".doctests"],
            get_variant_name("Doctests", host, python=python),
            python=python,
            host=host,
        )
    ]


def create_atlas_connect_variants():
    host = DEFAULT_HOST
    return [
        create_variant(
            [".atlas_connect"],
            get_variant_name("Atlas connect", host, python=python),
            python=python,
            host=host,
        )
        for python in MIN_MAX_PYTHON
    ]


def create_coverage_report_variants():
    return [create_variant(["coverage-report"], "Coverage Report", host=DEFAULT_HOST)]


def create_kms_variants():
    tasks = []
    tasks.append(EvgTaskRef(name="test-gcpkms", batchtime=BATCHTIME_WEEK))
    tasks.append("test-gcpkms-fail")
    tasks.append(EvgTaskRef(name="test-azurekms", batchtime=BATCHTIME_WEEK))
    tasks.append("test-azurekms-fail")
    return [create_variant(tasks, "KMS", host=HOSTS["debian11"])]


def create_import_time_variants():
    return [create_variant(["check-import-time"], "Import Time", host=DEFAULT_HOST)]


def create_backport_pr_variants():
    return [create_variant(["backport-pr"], "Backport PR", host=DEFAULT_HOST)]


def create_perf_variants():
    host = HOSTS["perf"]
    return [
        create_variant([".perf"], "Performance Benchmarks", host=host, batchtime=BATCHTIME_WEEK)
    ]


def create_aws_auth_variants():
    variants = []

    for host_name, python in product(["ubuntu20", "win64", "macos"], MIN_MAX_PYTHON):
        expansions = dict()
        tasks = [".auth-aws"]
        if host_name == "macos":
            tasks = [".auth-aws !.auth-aws-web-identity !.auth-aws-ecs !.auth-aws-ec2"]
        elif host_name == "win64":
            tasks = [".auth-aws !.auth-aws-ecs"]
        host = HOSTS[host_name]
        variant = create_variant(
            tasks,
            get_variant_name("Auth AWS", host, python=python),
            host=host,
            python=python,
            expansions=expansions,
        )
        variants.append(variant)
    return variants


def create_no_server_variants():
    host = HOSTS["rhel8"]
    return [create_variant([".no-server"], "No server", host=host)]


def create_alternative_hosts_variants():
    batchtime = BATCHTIME_WEEK
    variants = []

    host = HOSTS["rhel7"]
    variants.append(
        create_variant(
            [".5.0 .standalone !.sync_async"],
            get_variant_name("OpenSSL 1.0.2", host, python=CPYTHONS[0]),
            host=host,
            python=CPYTHONS[0],
            batchtime=batchtime,
        )
    )

    for host_name in OTHER_HOSTS:
        expansions = dict()
        handle_c_ext(C_EXTS[0], expansions)
        host = HOSTS[host_name]
        if "fips" in host_name.lower():
            expansions["REQUIRE_FIPS"] = "1"
        tags = [".6.0 .standalone !.sync_async"]
        if host_name == "Amazon2023":
            tags = [f".latest !.sync_async {t}" for t in SUB_TASKS]
        variants.append(
            create_variant(
                tags,
                display_name=get_variant_name("Other hosts", host),
                batchtime=batchtime,
                host=host,
                expansions=expansions,
            )
        )
    return variants


def create_aws_lambda_variants():
    host = HOSTS["rhel8"]
    return [create_variant([".aws_lambda"], display_name="FaaS Lambda", host=host)]


##############
# Tasks
##############


def create_server_version_tasks():
    tasks = []
    # Test all pythons with sharded_cluster, auth, and ssl.
    task_types = [(p, "sharded_cluster", "auth", "ssl") for p in ALL_PYTHONS]
    # Test all combinations of topology, auth, and ssl, with rotating pythons.
    for (topology, auth, ssl), python in zip_cycle(
        list(product(TOPOLOGIES, ["auth", "noauth"], ["ssl", "nossl"])), ALL_PYTHONS
    ):
        # Skip the ones we already have.
        if topology == "sharded_cluster" and auth == "auth" and ssl == "ssl":
            continue
        task_types.append((python, topology, auth, ssl))
    for python, topology, auth, ssl in task_types:
        tags = ["server-version", f"python-{python}", f"{topology}-{auth}-{ssl}"]
        expansions = dict(AUTH=auth, SSL=ssl, TOPOLOGY=topology)
        if python not in PYPYS:
            expansions["COVERAGE"] = "1"
        name = get_task_name("test", python=python, **expansions)
        server_func = FunctionCall(func="run server", vars=expansions)
        test_vars = expansions.copy()
        test_vars["PYTHON_VERSION"] = python
        test_func = FunctionCall(func="run tests", vars=test_vars)
        tasks.append(EvgTask(name=name, tags=tags, commands=[server_func, test_func]))
    return tasks


def create_standard_linux_tasks():
    tasks = []

    for (version, topology), python in zip_cycle(
        list(product(ALL_VERSIONS, TOPOLOGIES)), ALL_PYTHONS
    ):
        auth = "auth" if topology == "sharded_cluster" else "noauth"
        ssl = "nossl" if topology == "standalone" else "ssl"
        tags = [
            "standard-linux",
            f"server-{version}",
            f"python-{python}",
            f"{topology}-{auth}-{ssl}",
        ]
        expansions = dict(AUTH=auth, SSL=ssl, TOPOLOGY=topology, VERSION=version)
        name = get_task_name("test", python=python, **expansions)
        server_func = FunctionCall(func="run server", vars=expansions)
        test_vars = expansions.copy()
        test_vars["PYTHON_VERSION"] = python
        test_func = FunctionCall(func="run tests", vars=test_vars)
        tasks.append(EvgTask(name=name, tags=tags, commands=[server_func, test_func]))
    return tasks


def create_standard_non_linux_tasks():
    tasks = []

    for (version, topology), python, sync in zip_cycle(
        list(product(ALL_VERSIONS, TOPOLOGIES)), CPYTHONS, SYNCS
    ):
        auth = "auth" if topology == "sharded_cluster" else "noauth"
        ssl = "nossl" if topology == "standalone" else "ssl"
        tags = [
            "standard-non-linux",
            f"server-{version}",
            f"python-{python}",
            f"{topology}-{auth}-{ssl}",
            sync,
        ]
        expansions = dict(AUTH=auth, SSL=ssl, TOPOLOGY=topology, VERSION=version)
        name = get_task_name("test", python=python, sync=sync, **expansions)
        server_func = FunctionCall(func="run server", vars=expansions)
        test_vars = expansions.copy()
        test_vars["PYTHON_VERSION"] = python
        test_vars["TEST_NAME"] = f"default_{sync}"
        test_func = FunctionCall(func="run tests", vars=test_vars)
        tasks.append(EvgTask(name=name, tags=tags, commands=[server_func, test_func]))
    return tasks


def create_server_tasks():
    tasks = []
    for topo, version, (auth, ssl), sync in product(
        TOPOLOGIES, ALL_VERSIONS, AUTH_SSLS, [*SYNCS, "sync_async"]
    ):
        name = f"test-{version}-{topo}-{auth}-{ssl}-{sync}".lower()
        tags = [version, topo, auth, ssl, sync]
        server_vars = dict(
            VERSION=version,
            TOPOLOGY=topo if topo != "standalone" else "server",
            AUTH=auth,
            SSL=ssl,
        )
        server_func = FunctionCall(func="run server", vars=server_vars)
        test_vars = dict(AUTH=auth, SSL=ssl, SYNC=sync)
        if sync == "sync":
            test_vars["TEST_NAME"] = "default_sync"
        elif sync == "async":
            test_vars["TEST_NAME"] = "default_async"
        test_func = FunctionCall(func="run tests", vars=test_vars)
        tasks.append(EvgTask(name=name, tags=tags, commands=[server_func, test_func]))
    return tasks


def create_load_balancer_tasks():
    tasks = []
    for (auth, ssl), version in product(AUTH_SSLS, get_versions_from("6.0")):
        name = get_task_name(f"test-load-balancer-{auth}-{ssl}", version=version)
        tags = ["load-balancer", auth, ssl]
        server_vars = dict(
            TOPOLOGY="sharded_cluster",
            AUTH=auth,
            SSL=ssl,
            TEST_NAME="load_balancer",
            VERSION=version,
        )
        server_func = FunctionCall(func="run server", vars=server_vars)
        test_vars = dict(AUTH=auth, SSL=ssl, TEST_NAME="load_balancer")
        test_func = FunctionCall(func="run tests", vars=test_vars)
        tasks.append(EvgTask(name=name, tags=tags, commands=[server_func, test_func]))

    return tasks


def create_kms_tasks():
    tasks = []
    for kms_type in ["gcp", "azure"]:
        for success in [True, False]:
            name = f"test-{kms_type}kms"
            sub_test_name = kms_type
            if not success:
                name += "-fail"
                sub_test_name += "-fail"
            commands = []
            if not success:
                commands.append(FunctionCall(func="run server"))
            test_vars = dict(TEST_NAME="kms", SUB_TEST_NAME=sub_test_name)
            test_func = FunctionCall(func="run tests", vars=test_vars)
            commands.append(test_func)
            tasks.append(EvgTask(name=name, commands=commands))
    return tasks


def create_aws_tasks():
    tasks = []
    aws_test_types = [
        "regular",
        "assume-role",
        "ec2",
        "env-creds",
        "session-creds",
        "web-identity",
        "ecs",
    ]
    for version in get_versions_from("4.4"):
        base_name = f"test-auth-aws-{version}"
        base_tags = ["auth-aws"]
        server_vars = dict(AUTH_AWS="1", VERSION=version)
        server_func = FunctionCall(func="run server", vars=server_vars)
        assume_func = FunctionCall(func="assume ec2 role")
        for test_type in aws_test_types:
            tags = [*base_tags, f"auth-aws-{test_type}"]
            name = f"{base_name}-{test_type}"
            test_vars = dict(TEST_NAME="auth_aws", SUB_TEST_NAME=test_type)
            test_func = FunctionCall(func="run tests", vars=test_vars)
            funcs = [server_func, assume_func, test_func]
            tasks.append(EvgTask(name=name, tags=tags, commands=funcs))

        tags = [*base_tags, "auth-aws-web-identity"]
        name = f"{base_name}-web-identity-session-name"
        test_vars = dict(
            TEST_NAME="auth_aws", SUB_TEST_NAME="web-identity", AWS_ROLE_SESSION_NAME="test"
        )
        test_func = FunctionCall(func="run tests", vars=test_vars)
        funcs = [server_func, assume_func, test_func]
        tasks.append(EvgTask(name=name, tags=tags, commands=funcs))

    return tasks


def create_oidc_tasks():
    tasks = []
    for sub_test in ["default", "azure", "gcp", "eks", "aks", "gke"]:
        vars = dict(TEST_NAME="auth_oidc", SUB_TEST_NAME=sub_test)
        test_func = FunctionCall(func="run tests", vars=vars)
        task_name = f"test-auth-oidc-{sub_test}"
        tags = ["auth_oidc"]
        if sub_test != "default":
            tags.append("auth_oidc_remote")
        tasks.append(EvgTask(name=task_name, tags=tags, commands=[test_func]))
    return tasks


def create_mod_wsgi_tasks():
    tasks = []
    for test, topology in product(["standalone", "embedded-mode"], ["standalone", "replica_set"]):
        if test == "standalone":
            task_name = "mod-wsgi-"
        else:
            task_name = "mod-wsgi-embedded-mode-"
        task_name += topology.replace("_", "-")
        server_vars = dict(TOPOLOGY=topology)
        server_func = FunctionCall(func="run server", vars=server_vars)
        vars = dict(TEST_NAME="mod_wsgi", SUB_TEST_NAME=test.split("-")[0])
        test_func = FunctionCall(func="run tests", vars=vars)
        tags = ["mod_wsgi"]
        commands = [server_func, test_func]
        tasks.append(EvgTask(name=task_name, tags=tags, commands=commands))
    return tasks


def _create_ocsp_tasks(algo, variant, server_type, base_task_name):
    tasks = []
    file_name = f"{algo}-basic-tls-ocsp-{variant}.json"

    for version in get_versions_from("4.4"):
        if version == "latest":
            python = MIN_MAX_PYTHON[-1]
        else:
            python = MIN_MAX_PYTHON[0]

        vars = dict(
            ORCHESTRATION_FILE=file_name,
            OCSP_SERVER_TYPE=server_type,
            TEST_NAME="ocsp",
            PYTHON_VERSION=python,
            VERSION=version,
        )
        test_func = FunctionCall(func="run tests", vars=vars)

        tags = ["ocsp", f"ocsp-{algo}", version]
        if "disableStapling" not in variant:
            tags.append("ocsp-staple")

        task_name = get_task_name(
            f"test-ocsp-{algo}-{base_task_name}", python=python, version=version
        )
        tasks.append(EvgTask(name=task_name, tags=tags, commands=[test_func]))
    return tasks


def create_aws_lambda_tasks():
    assume_func = FunctionCall(func="assume ec2 role")
    vars = dict(TEST_NAME="aws_lambda")
    test_func = FunctionCall(func="run tests", vars=vars)
    task_name = "test-aws-lambda-deployed"
    tags = ["aws_lambda"]
    commands = [assume_func, test_func]
    return [EvgTask(name=task_name, tags=tags, commands=commands)]


def create_search_index_tasks():
    assume_func = FunctionCall(func="assume ec2 role")
    server_func = FunctionCall(func="run server", vars=dict(TEST_NAME="search_index"))
    vars = dict(TEST_NAME="search_index")
    test_func = FunctionCall(func="run tests", vars=vars)
    task_name = "test-search-index-helpers"
    tags = ["search_index"]
    commands = [assume_func, server_func, test_func]
    return [EvgTask(name=task_name, tags=tags, commands=commands)]


def create_atlas_connect_tasks():
    vars = dict(TEST_NAME="atlas_connect")
    assume_func = FunctionCall(func="assume ec2 role")
    test_func = FunctionCall(func="run tests", vars=vars)
    task_name = "test-atlas-connect"
    tags = ["atlas_connect"]
    return [EvgTask(name=task_name, tags=tags, commands=[assume_func, test_func])]


def create_enterprise_auth_tasks():
    tasks = []
    for python in [*MIN_MAX_PYTHON, PYPYS[-1]]:
        vars = dict(TEST_NAME="enterprise_auth", AUTH="auth", PYTHON_VERSION=python)
        server_func = FunctionCall(func="run server", vars=vars)
        assume_func = FunctionCall(func="assume ec2 role")
        test_func = FunctionCall(func="run tests", vars=vars)
        task_name = get_task_name("test-enterprise-auth", python=python)
        tags = ["enterprise_auth"]
        if python in PYPYS:
            tags += ["pypy"]
        tasks.append(
            EvgTask(name=task_name, tags=tags, commands=[server_func, assume_func, test_func])
        )
    return tasks


def create_perf_tasks():
    tasks = []
    for version, ssl, sync in product(["8.0"], ["ssl", "nossl"], ["sync", "async"]):
        vars = dict(VERSION=f"v{version}-perf", SSL=ssl)
        server_func = FunctionCall(func="run server", vars=vars)
        vars = dict(TEST_NAME="perf", SUB_TEST_NAME=sync)
        test_func = FunctionCall(func="run tests", vars=vars)
        attach_func = FunctionCall(func="attach benchmark test results")
        send_func = FunctionCall(func="send dashboard data")
        task_name = f"perf-{version}-standalone"
        if ssl == "ssl":
            task_name += "-ssl"
        if sync == "async":
            task_name += "-async"
        tags = ["perf"]
        commands = [server_func, test_func, attach_func, send_func]
        tasks.append(EvgTask(name=task_name, tags=tags, commands=commands))
    return tasks


def create_atlas_data_lake_tasks():
    tags = ["atlas_data_lake"]
    tasks = []
    for c_ext in C_EXTS:
        vars = dict(TEST_NAME="data_lake")
        handle_c_ext(c_ext, vars)
        test_func = FunctionCall(func="run tests", vars=vars)
        task_name = f"test-atlas-data-lake-{c_ext}"
        tasks.append(EvgTask(name=task_name, tags=tags, commands=[test_func]))
    return tasks


def create_getdata_tasks():
    # Wildcard task. Do you need to find out what tools are available and where?
    # Throw it here, and execute this task on all buildvariants
    cmd = get_subprocess_exec(args=[".evergreen/scripts/run-getdata.sh"])
    return [EvgTask(name="getdata", commands=[cmd])]


def create_coverage_report_tasks():
    tags = ["coverage"]
    task_name = "coverage-report"
    # BUILD-3165: We can't use "*" (all tasks) and specify "variant".
    # Instead list out all coverage tasks using tags.
    # Run the coverage task even if some tasks fail.
    # Run the coverage task even if some tasks are not scheduled in a patch build.
    task_deps = [
        EvgTaskDependency(
            name=".server-version", variant=".coverage_tag", status="*", patch_optional=True
        )
    ]
    cmd = FunctionCall(func="download and merge coverage")
    return [EvgTask(name=task_name, tags=tags, depends_on=task_deps, commands=[cmd])]


def create_import_time_tasks():
    name = "check-import-time"
    tags = ["pr"]
    args = [".evergreen/scripts/check-import-time.sh", "${revision}", "${github_commit}"]
    cmd = get_subprocess_exec(args=args)
    return [EvgTask(name=name, tags=tags, commands=[cmd])]


def create_backport_pr_tasks():
    name = "backport-pr"
    args = [
        "${DRIVERS_TOOLS}/.evergreen/github_app/backport-pr.sh",
        "mongodb",
        "mongo-python-driver",
        "${github_commit}",
    ]
    cmd = get_subprocess_exec(args=args)
    return [EvgTask(name=name, commands=[cmd], allowed_requesters=["commit"])]


def create_ocsp_tasks():
    tasks = []
    tests = [
        ("disableStapling", "valid", "valid-cert-server-does-not-staple"),
        ("disableStapling", "revoked", "invalid-cert-server-does-not-staple"),
        ("disableStapling", "valid-delegate", "delegate-valid-cert-server-does-not-staple"),
        ("disableStapling", "revoked-delegate", "delegate-invalid-cert-server-does-not-staple"),
        ("disableStapling", "no-responder", "soft-fail"),
        ("mustStaple", "valid", "valid-cert-server-staples"),
        ("mustStaple", "revoked", "invalid-cert-server-staples"),
        ("mustStaple", "valid-delegate", "delegate-valid-cert-server-staples"),
        ("mustStaple", "revoked-delegate", "delegate-invalid-cert-server-staples"),
        (
            "mustStaple-disableStapling",
            "revoked",
            "malicious-invalid-cert-mustStaple-server-does-not-staple",
        ),
        (
            "mustStaple-disableStapling",
            "revoked-delegate",
            "delegate-malicious-invalid-cert-mustStaple-server-does-not-staple",
        ),
        (
            "mustStaple-disableStapling",
            "no-responder",
            "malicious-no-responder-mustStaple-server-does-not-staple",
        ),
    ]
    for algo in ["ecdsa", "rsa"]:
        for variant, server_type, base_task_name in tests:
            new_tasks = _create_ocsp_tasks(algo, variant, server_type, base_task_name)
            tasks.extend(new_tasks)

    return tasks


def create_mockupdb_tasks():
    test_func = FunctionCall(func="run tests", vars=dict(TEST_NAME="mockupdb"))
    task_name = "test-mockupdb"
    tags = ["mockupdb"]
    return [EvgTask(name=task_name, tags=tags, commands=[test_func])]


def create_doctest_tasks():
    server_func = FunctionCall(func="run server")
    test_func = FunctionCall(func="run just script", vars=dict(JUSTFILE_TARGET="docs-test"))
    task_name = "test-doctests"
    tags = ["doctests"]
    return [EvgTask(name=task_name, tags=tags, commands=[server_func, test_func])]


def create_no_server_tasks():
    test_func = FunctionCall(func="run tests")
    task_name = "test-no-server"
    tags = ["no-server"]
    return [EvgTask(name=task_name, tags=tags, commands=[test_func])]


def create_free_threading_tasks():
    vars = dict(VERSION="8.0", TOPOLOGY="replica_set")
    server_func = FunctionCall(func="run server", vars=vars)
    test_func = FunctionCall(func="run tests")
    task_name = "test-free-threading"
    tags = ["free-threading"]
    return [EvgTask(name=task_name, tags=tags, commands=[server_func, test_func])]


def create_serverless_tasks():
    vars = dict(TEST_NAME="serverless", AUTH="auth", SSL="ssl")
    test_func = FunctionCall(func="run tests", vars=vars)
    tags = ["serverless"]
    task_name = "test-serverless"
    return [EvgTask(name=task_name, tags=tags, commands=[test_func])]


##############
# Functions
##############


def create_upload_coverage_func():
    # Upload the coverage report for all tasks in a single build to the same directory.
    remote_file = (
        "coverage/${revision}/${version_id}/coverage/coverage.${build_variant}.${task_name}"
    )
    display_name = "Raw Coverage Report"
    cmd = get_s3_put(
        local_file="src/.coverage",
        remote_file=remote_file,
        display_name=display_name,
        content_type="text/html",
    )
    return "upload coverage", [get_assume_role(), cmd]


def create_download_and_merge_coverage_func():
    include_expansions = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
    args = [
        ".evergreen/scripts/download-and-merge-coverage.sh",
        "${bucket_name}",
        "${revision}",
        "${version_id}",
    ]
    merge_cmd = get_subprocess_exec(
        silent=True, include_expansions_in_env=include_expansions, args=args
    )
    combine_cmd = get_subprocess_exec(args=[".evergreen/combine-coverage.sh"])
    # Upload the resulting html coverage report.
    args = [
        ".evergreen/scripts/upload-coverage-report.sh",
        "${bucket_name}",
        "${revision}",
        "${version_id}",
    ]
    upload_cmd = get_subprocess_exec(
        silent=True, include_expansions_in_env=include_expansions, args=args
    )
    display_name = "Coverage Report HTML"
    remote_file = "coverage/${revision}/${version_id}/htmlcov/index.html"
    put_cmd = get_s3_put(
        local_file="src/htmlcov/index.html",
        remote_file=remote_file,
        display_name=display_name,
        content_type="text/html",
    )
    cmds = [get_assume_role(), merge_cmd, combine_cmd, upload_cmd, put_cmd]
    return "download and merge coverage", cmds


def create_upload_mo_artifacts_func():
    include = ["./**.core", "./**.mdmp"]  # Windows: minidumps
    archive_cmd = archive_targz_pack(target="mongo-coredumps.tgz", source_dir="./", include=include)
    display_name = "Core Dumps - Execution"
    remote_file = "${build_variant}/${revision}/${version_id}/${build_id}/coredumps/${task_id}-${execution}-mongodb-coredumps.tar.gz"
    s3_dumps = get_s3_put(
        local_file="mongo-coredumps.tgz", remote_file=remote_file, display_name=display_name
    )
    display_name = "drivers-tools-logs.tar.gz"
    remote_file = "${build_variant}/${revision}/${version_id}/${build_id}/logs/${task_id}-${execution}-drivers-tools-logs.tar.gz"
    s3_logs = get_s3_put(
        local_file="${DRIVERS_TOOLS}/.evergreen/test_logs.tar.gz",
        remote_file=remote_file,
        display_name=display_name,
    )
    cmds = [get_assume_role(), archive_cmd, s3_dumps, s3_logs]
    return "upload mo artifacts", cmds


def create_fetch_source_func():
    # Executes clone and applies the submitted patch, if any.
    cmd = git_get_project(directory="src")
    return "fetch source", [cmd]


def create_setup_system_func():
    # Make an evergreen expansion file with dynamic values.
    includes = ["is_patch", "project", "version_id"]
    args = [".evergreen/scripts/setup-system.sh"]
    setup_cmd = get_subprocess_exec(include_expansions_in_env=includes, args=args)
    # Load the expansion file to make an evergreen variable with the current unique version.
    expansion_cmd = expansions_update(file="src/expansion.yml")
    return "setup system", [setup_cmd, expansion_cmd]


def create_upload_test_results_func():
    results_cmd = attach_results(file_location="${DRIVERS_TOOLS}/results.json")
    xresults_cmd = attach_xunit_results(file="src/xunit-results/TEST-*.xml")
    return "upload test results", [results_cmd, xresults_cmd]


def create_run_server_func():
    includes = [
        "VERSION",
        "TOPOLOGY",
        "AUTH",
        "SSL",
        "ORCHESTRATION_FILE",
        "PYTHON_BINARY",
        "PYTHON_VERSION",
        "STORAGE_ENGINE",
        "REQUIRE_API_VERSION",
        "DRIVERS_TOOLS",
        "TEST_CRYPT_SHARED",
        "AUTH_AWS",
        "LOAD_BALANCER",
        "LOCAL_ATLAS",
        "NO_EXT",
    ]
    args = [".evergreen/just.sh", "run-server", "${TEST_NAME}"]
    sub_cmd = get_subprocess_exec(include_expansions_in_env=includes, args=args)
    expansion_cmd = expansions_update(file="${DRIVERS_TOOLS}/mo-expansion.yml")
    return "run server", [sub_cmd, expansion_cmd]


def create_run_just_script_func():
    includes = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
    args = [".evergreen/just.sh", "${JUSTFILE_TARGET}"]
    cmd = get_subprocess_exec(include_expansions_in_env=includes, args=args)
    return "run just script", [cmd]


def create_run_tests_func():
    includes = [
        "AUTH",
        "SSL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "COVERAGE",
        "PYTHON_BINARY",
        "LIBMONGOCRYPT_URL",
        "MONGODB_URI",
        "PYTHON_VERSION",
        "DISABLE_TEST_COMMANDS",
        "GREEN_FRAMEWORK",
        "NO_EXT",
        "COMPRESSORS",
        "MONGODB_API_VERSION",
        "DEBUG_LOG",
        "ORCHESTRATION_FILE",
        "OCSP_SERVER_TYPE",
        "VERSION",
        "IS_WIN32",
        "REQUIRE_FIPS",
    ]
    args = [".evergreen/just.sh", "setup-tests", "${TEST_NAME}", "${SUB_TEST_NAME}"]
    setup_cmd = get_subprocess_exec(include_expansions_in_env=includes, args=args)
    test_cmd = get_subprocess_exec(args=[".evergreen/just.sh", "run-tests"])
    return "run tests", [setup_cmd, test_cmd]


def create_cleanup_func():
    cmd = get_subprocess_exec(args=[".evergreen/scripts/cleanup.sh"])
    return "cleanup", [cmd]


def create_teardown_system_func():
    tests_cmd = get_subprocess_exec(args=[".evergreen/just.sh", "teardown-tests"])
    drivers_cmd = get_subprocess_exec(args=["${DRIVERS_TOOLS}/.evergreen/teardown.sh"])
    return "teardown system", [tests_cmd, drivers_cmd]


def create_assume_ec2_role_func():
    cmd = ec2_assume_role(role_arn="${aws_test_secrets_role}", duration_seconds=3600)
    return "assume ec2 role", [cmd]


def create_attach_benchmark_test_results_func():
    cmd = attach_results(file_location="src/report.json")
    return "attach benchmark test results", [cmd]


def create_send_dashboard_data_func():
    cmd = perf_send(file="src/results.json")
    return "send dashboard data", [cmd]


mod = sys.modules[__name__]
write_variants_to_file(mod)
write_tasks_to_file(mod)
write_functions_to_file(mod)
