"""HY2 migration tests. All addresses and credentials below are fixtures."""
import base64
import contextlib
import copy
from dataclasses import asdict
import datetime as dt
from io import BytesIO
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

# Only the Windows test interpreter lacks fcntl; no test acquires OS locks.
if os.name == "nt":
    from types import ModuleType
    sys.modules.setdefault("fcntl", ModuleType("fcntl"))

import hy2qianyi as q


def node(tag, port, *, quota=False, iplimit=False, nat=False, label="source"):
    main = tag == "main"
    domain = "migration.example.org"
    password = f"{label}-{port}-fixture-password"
    meta = dict(STATE_SCHEMA="1", HY_DOMAIN=domain, MASQ_URL="https://www.example.org/",
                ENABLE_SALAMANDER="0", SALAMANDER_PASSWORD="")
    if main:
        meta.update(MAIN_PORT=str(port), HY_LISTEN=f"0.0.0.0:{port}",
                    MAIN_PASSWORD=password, MAIN_PASSWORD_FILE=q.MAIN_FILES["password"],
                    TLS_CERT=f"/etc/hysteria/certs/{domain}/fullchain.pem",
                    TLS_KEY=f"/etc/hysteria/certs/{domain}/privkey.pem", NODE_NAME="Fixture main")
    else:
        created = int(time.time()) - 10
        meta.update(TAG=tag, ID=tag.removeprefix("hy2-temp-"), PORT=str(port),
                    PASSWORD=password, PUBLIC_DOMAIN=domain, SERVER_ADDR=domain,
                    IP_VERSION="4", LISTEN_ADDR=f"0.0.0.0:{port}",
                    CREATE_EPOCH=str(created), EXPIRE_EPOCH=str(created + 7200),
                    DURATION_SECONDS="7200", LANDING="nat" if nat else "local",
                    PQ_LIMIT_BYTES="100" if quota else "", IP_LIMIT="2" if iplimit else "0",
                    IP_STICKY_SECONDS="120")
    if nat:
        meta.update(WG_IF="wg-nat", MARK="2333", TABLE_ID="100", RULE_PRIORITY="31000",
                    OIF_RULE_PRIORITY="30999", HANDSHAKE_MAX="180")
    title = meta["NODE_NAME"] if main else tag
    url = f"hy2://{quote(password, safe='')}@{domain}:{port}/?sni={domain}#{quote(title)}"
    owner = dict(PORT=str(port), OWNER_KIND="manual" if main else "temp", OWNER_TAG="" if main else tag)
    limits = dict(owner, ORIGINAL_LIMIT_BYTES="100", SAVED_USED_BYTES="60", LIMIT_BYTES="40",
                  REBUILD_PENDING="0", RESET_INTERVAL_SECONDS="2592000", NEXT_RESET_EPOCH="9999999999") if quota else None
    slots = dict(owner, IP_VERSION="4", IP_LIMIT="2", IP_STICKY_SECONDS="120") if iplimit else None
    result = q.Node(tag, port, meta, "", url, limits, slots)
    result.config = render(result)
    return result


def render(value):
    def quoted(text):
        return "'" + text.replace("'", "''") + "'"
    meta = value.meta
    domain = meta["HY_DOMAIN"]
    listen = meta["HY_LISTEN"] if value.main else meta["LISTEN_ADDR"]
    password = meta["MAIN_PASSWORD"] if value.main else meta["PASSWORD"]
    result = (f"listen: {quoted(listen)}\n\n"
              f"tls:\n  cert: '/etc/hysteria/certs/{domain}/fullchain.pem'\n"
              f"  key: '/etc/hysteria/certs/{domain}/privkey.pem'\n\n"
              f"auth:\n  type: password\n  password: {quoted(password)}\n\n")
    if meta["ENABLE_SALAMANDER"] == "1":
        result += ("obfs:\n  type: salamander\n  salamander:\n"
                   f"    password: {quoted(meta['SALAMANDER_PASSWORD'])}\n\n")
    if value.nat:
        result += ("outbounds:\n  - name: nat\n    type: direct\n    direct:\n"
                   f"      mode: 4\n      bindDevice: {quoted(meta['WG_IF'])}\n\n")
    result += ("masquerade:\n  type: proxy\n  proxy:\n"
               f"    url: {quoted(meta['MASQ_URL'])}\n    rewriteHost: true\n\n"
               "speedTest: false\ndisableUDP: false\nudpIdleTimeout: 60s\n")
    return result


def packet(nodes, origin="a" * 64, **extra):
    return q.encode_json(dict(format=q.FORMAT, origin=origin, profile={"test": "profile"},
                              nodes=[asdict(n) for n in nodes], blacklist=[], daily=None, **extra))


class FakeHost(q.Host):
    def __init__(self, work):
        super().__init__(work)
        self.active_units = set()
        self.enabled_units = set()
        self.fences = set()
        self.actions = []
        self.fail_start = None
        self.fail_publish = False
        self.foreign = {}
        self.restored_slots = []

    @contextlib.contextmanager
    def lock(self, *_names):
        yield

    def identity(self):
        return "b" * 64

    def profile(self, _nat):
        return {"test": "profile"}

    def test_config(self, value):
        q.validate_node(value)

    def check_unit(self, value):
        if not Path(q.SYSTEMD, value.unit).is_file():
            raise q.MigrationError("missing fixture unit")

    def property(self, unit, name):
        path = Path(q.SYSTEMD) / unit
        if name == "LoadState":
            return "loaded" if path.is_file() else "not-found"
        if name == "FragmentPath":
            return str(path) if path.is_file() else ""
        return ""

    def active(self, unit):
        return unit in self.active_units

    def enabled(self, unit):
        return unit in self.enabled_units

    def sys(self, action, *units, **_kwargs):
        self.actions.append((action, *units))
        if action == "start":
            for unit in units:
                if unit == self.fail_start:
                    self.fail_start = None
                    raise q.MigrationError("simulated start failure")
                self.active_units.add(unit)
        elif action == "stop":
            self.active_units.difference_update(units)
        elif action == "enable":
            self.enabled_units.update(units)
        elif action == "disable":
            self.enabled_units.difference_update(units)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def listeners(self, ports):
        listeners = dict(self.foreign)
        for entry in self.inventory().values():
            if entry.unit in self.active_units:
                listeners[entry.port] = {entry.unit}
        return {port: listeners[port] for port in ports if port in listeners}

    def owns(self, unit, pids):
        return pids == {unit}

    def wait_ready(self, nodes):
        by_port = {value.port: value for value in nodes}
        if set(self.inspect_owners(set(by_port), by_port)) != set(by_port):
            raise q.MigrationError("simulated listener check")

    def check_optional_policies(self, _ports):
        pass

    def adapt_nat(self, value):
        if value.nat:
            value.meta.update(MARK="8080", TABLE_ID="200", RULE_PRIORITY="31001", OIF_RULE_PRIORITY="31000")

    def capture_ip_slots(self, value):
        return [["192.0.2.5", int(time.time()) + 60]] if value.iplimit else []

    def restore_ip_slots(self, value, slots):
        if slots:
            self.restored_slots.append((value.port, slots))

    def native(self, script, *args):
        if script.startswith("for port;"):
            for port in args:
                for path in q.policy_paths(port).values():
                    q.remove_file(path)
        if script.startswith("hy2_pq_snapshot_unlocked"):
            limit = q.env_decode(Path(q.policy_paths(args[0])["quota"]).read_bytes())
            return f"active|{limit['ORIGINAL_LIMIT_BYTES']}|{limit['SAVED_USED_BYTES']}|{limit['LIMIT_BYTES']}"
        if script.startswith("hy2_il"):
            return "active"
        return ""

    def policy_state(self, value):
        q.validate_policies(value)

    def tables(self):
        return set(self.fences)

    def source_blacklist(self, _ports):
        return set()

    def source_daily(self, _nodes):
        return None

    def fence(self, table, ports):
        self.actions.append(("fence", *sorted(ports)))
        self.fences.add(table)

    def unfence(self, table):
        self.actions.append(("unfence",))
        self.fences.discard(table)

    def clear_guards(self, table, units):
        if self.fail_publish:
            self.fail_publish = False
            raise q.MigrationError("simulated cleanup failure")
        super().clear_guards(table, units)

    def refresh_subscription(self):
        self.actions.append(("refresh",))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        state, systemd, cfg, run = (root / name for name in ("state", "systemd", "config", "run"))
        for path in (state / "temp", state / "main", state / "quota", state / "iplimit", systemd, cfg / "temp", run):
            path.mkdir(parents=True)
        main = {"meta": str(state / "main/main.env"), "config": str(cfg / "main.yaml"),
                "url": str(root / "main.url"), "password": str(state / "main/main.password")}
        updates = dict(STATE=str(state), LOCK_DIR=str(run), SYSTEMD=str(systemd), XRAY_DIR=str(cfg),
                       MAIN_FILES=main, MAIN_SUB=str(root / "main.sub"), MAIN_DEFAULTS=str(root / "defaults"),
                       JOURNAL=str(state / ".hy2qianyi-recovery.json"), LEGACY={},
                       permit_file=lambda table: run / f"{table}.permit")
        self.enterContext(patch.multiple(q, **updates))
        real_paths = q.Node.paths
        def local_paths(value):
            paths = real_paths(value)
            if not value.main:
                paths["config"] = str(cfg / "temp" / f"{value.tag}.yaml")
            return paths
        self.enterContext(patch.object(q.Node, "paths", local_paths))
        self.enterContext(patch.object(q, "secure_read", lambda path, *args: Path(path).read_bytes()))
        self.host = FakeHost(run)
        self.defaults = b"HY_DOMAIN=migration.example.org\nHY_IPV6_DOMAIN=target6.example.org\n"
        Path(q.MAIN_DEFAULTS).write_bytes(self.defaults)
        self.main = node("main", 443, label="target")
        self.old = node("hy2-temp-old", 40000, quota=True, iplimit=True, label="target")
        self.keep = node("hy2-temp-keep", 40001, label="target")
        (systemd / "hy2.service").write_text("[Service]\n")
        self.host.install([self.main, self.old, self.keep])
        self.host.active_units.update(n.unit for n in (self.main, self.old, self.keep))
        self.host.enabled_units.update(self.host.active_units)

    def current(self, port):
        return self.host.read_node(self.host.inventory()[port])

    def assert_clean(self):
        self.assertFalse(Path(q.JOURNAL).exists())
        self.assertFalse(self.host.fences)
        self.assertFalse(list(Path(q.SYSTEMD).glob("*.service.d")))
        self.assertFalse(list(self.host.work.glob("*.permit")))
        self.assertFalse(list(Path(q.STATE).rglob("*.hy2qianyi-new")))

    def test_replace_matching_port_keep_disjoint_and_remap_tag(self):
        incoming = node("main", 443, quota=True)
        replacing = node("hy2-temp-keep", 40000, quota=True, iplimit=True)
        additional = node("hy2-temp-add", 40002)
        q.apply_bundle(self.host, packet([incoming, replacing, additional]))
        self.assertEqual(self.current(443).url, incoming.url)
        self.assertEqual(Path(q.MAIN_FILES["password"]).read_text().strip(), incoming.meta["MAIN_PASSWORD"])
        self.assertEqual(Path(q.MAIN_SUB).read_bytes(), base64.b64encode(incoming.url.encode()))
        self.assertEqual(self.current(40001).url, self.keep.url)
        self.assertEqual(self.current(40000).url, replacing.url)
        self.assertNotEqual(self.current(40000).tag, self.keep.tag)
        self.assertEqual(self.current(40000).quota["OWNER_TAG"], self.current(40000).tag)
        self.assertEqual(self.current(40000).quota["LIMIT_BYTES"], "40")
        self.assertEqual(self.current(40002).url, additional.url)
        self.assertFalse(any(self.keep.unit in action[1:] for action in self.host.actions))
        self.assert_clean()

    def test_failed_start_restores_main_password_limits_and_ip_slots(self):
        new = node("hy2-temp-new", 40000)
        self.host.fail_start = new.unit
        with self.assertRaisesRegex(q.MigrationError, "simulated start failure"):
            q.apply_bundle(self.host, packet([node("main", 443), new, node("hy2-temp-add", 40002)]))
        self.assertEqual(self.current(443).url, self.main.url)
        self.assertEqual(Path(q.MAIN_FILES["password"]).read_text().strip(), self.main.meta["MAIN_PASSWORD"])
        self.assertEqual(Path(q.MAIN_DEFAULTS).read_bytes(), self.defaults)
        self.assertEqual(self.current(40000).url, self.old.url)
        self.assertEqual(self.current(40000).quota["LIMIT_BYTES"], "40")
        self.assertEqual(self.host.restored_slots[0][0], 40000)
        self.assertNotIn(40002, self.host.inventory())
        self.assertEqual(self.host.active_units, {n.unit for n in (self.main, self.old, self.keep)})
        self.assert_clean()

    def test_publish_recovery_keeps_post_commit_quota_usage(self):
        new = node("hy2-temp-new", 40000, quota=True)
        self.host.fail_publish = True
        with self.assertRaises(q.MigrationError):
            q.apply_bundle(self.host, packet([node("main", 443), new]))
        self.assertEqual(q.journal_read()["phase"], "publish")
        live = dict(new.quota, SAVED_USED_BYTES="70", LIMIT_BYTES="30")
        q.write_file(q.policy_paths(40000)["quota"], q.env_encode(live))
        q.recover_target(self.host)
        self.assertEqual(self.current(40000).quota["LIMIT_BYTES"], "30")
        self.assert_clean()

    def test_foreign_listener_rejected_before_mutation(self):
        self.host.foreign[40002] = {"unrelated-service"}
        with self.assertRaises(q.MigrationError):
            q.apply_bundle(self.host, packet([node("main", 443), node("hy2-temp-new", 40002)]))
        self.assertEqual(self.host.actions, [])
        self.assertEqual(self.current(443).url, self.main.url)
        self.assert_clean()

    def test_same_machine_rejected_before_mutation(self):
        with self.assertRaisesRegex(q.MigrationError, "machine-id"):
            q.apply_bundle(self.host, packet([node("main", 443)], origin=self.host.identity()))
        self.assertEqual(self.host.actions, [])
        self.assert_clean()

    def test_expired_transfer_node_preserves_existing_target_node(self):
        old = node("hy2-temp-expiring", 40000)
        old.meta["EXPIRE_EPOCH"] = "1"
        q.apply_bundle(self.host, packet([node("main", 443), old]))
        self.assertEqual(self.current(40000).url, self.old.url)
        self.assert_clean()

    def test_old_temp_stop_marker_covers_preflight_replacement_and_publication(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        other_marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.keep.tag}"
        events = []
        locked = False

        @contextlib.contextmanager
        def track_lock(*names):
            nonlocal locked
            if names == ("temp.lock",):
                self.assertFalse(locked)
                locked = True
                try:
                    yield
                finally:
                    self.assertFalse(marker.exists())
                    locked = False
            else:
                self.assertTrue(locked)
                self.assertEqual(marker.read_text(), f"{os.getpid()}\n")
                yield

        def checked(name, original):
            def call(*args, **kwargs):
                self.assertTrue(locked, name)
                self.assertEqual(marker.read_text(), f"{os.getpid()}\n", name)
                self.assertFalse(other_marker.exists(), name)
                events.append(name)
                return original(*args, **kwargs)
            return call

        phases = ("profile", "fence", "guard_units", "clear_policies", "remove_temp_nodes",
                  "install", "sys", "wait_ready", "refresh_subscription", "clear_guards", "unfence")
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(self.host, "lock", track_lock))
            for name in phases:
                stack.enter_context(patch.object(self.host, name, checked(name, getattr(self.host, name))))
            q.apply_bundle(self.host, packet([node("main", 443), node("hy2-temp-new", 40000)]))
        self.assertEqual(set(events), set(phases))
        self.assertFalse(locked)
        self.assertFalse(list(Path(q.LOCK_DIR).glob("stoppost-preserve.*")))
        self.assert_clean()

    def test_nested_stop_reuses_same_pid_marker_without_removing_outer_protection(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        with q.preserve_stops(self.host, {443, self.old.port}):
            original = marker.read_bytes()
            stop = self.host.sys
            def stop_while_protected(*args, **kwargs):
                self.assertEqual(marker.read_bytes(), original)
                return stop(*args, **kwargs)
            with patch.object(self.host, "sys", side_effect=stop_while_protected):
                q.stop_defined(self.host, {self.main.unit, self.old.unit})
                self.assertEqual(marker.read_bytes(), original)
                q.stop_defined(self.host, {self.old.unit})
                self.assertEqual(marker.read_bytes(), original)
        self.assertFalse(marker.exists())
        self.assertNotIn(self.old.unit, self.host.active_units)

    def test_stop_markers_are_removed_after_failure_in_the_protected_scope(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        with self.assertRaisesRegex(RuntimeError, "protected failure"):
            with q.preserve_stops(self.host, {self.old.port}):
                self.assertTrue(marker.exists())
                raise RuntimeError("protected failure")
        self.assertFalse(marker.exists())
        with patch.object(self.host, "sys", side_effect=q.MigrationError("stop failed")):
            with self.assertRaisesRegex(q.MigrationError, "stop failed"):
                q.stop_defined(self.host, {self.old.unit})
        self.assertFalse(marker.exists())

    def test_nested_stop_failure_leaves_outer_marker_until_scope_exits(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        with q.preserve_stops(self.host, {self.old.port}):
            with patch.object(self.host, "sys", side_effect=q.MigrationError("stop failed")):
                with self.assertRaisesRegex(q.MigrationError, "stop failed"):
                    q.stop_defined(self.host, {self.old.unit})
            self.assertEqual(marker.read_text(), f"{os.getpid()}\n")
        self.assertFalse(marker.exists())

    def test_stale_pid_stop_markers_are_replaced_and_scoped(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        stale = os.getpid() + 100000
        for operation in ("preserve", "stop"):
            with self.subTest(operation=operation):
                marker.write_text(f"{stale}\n")
                with patch.object(q.os, "kill", side_effect=ProcessLookupError) as probe:
                    if operation == "preserve":
                        with q.preserve_stops(self.host, {self.old.port}):
                            self.assertEqual(marker.read_text(), f"{os.getpid()}\n")
                    else:
                        with patch.object(self.host, "sys") as stop:
                            def observe(*args, **kwargs):
                                self.assertEqual(marker.read_text(), f"{os.getpid()}\n")
                            stop.side_effect = observe
                            q.stop_defined(self.host, {self.old.unit})
                        stop.assert_called_once()
                    probe.assert_called_once_with(stale, 0)
                self.assertFalse(marker.exists())

    def test_live_foreign_pid_stop_markers_are_preserved_and_stop_is_refused(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        other_pid = os.getpid() + 100000
        before = f"{other_pid}\n".encode()
        marker.write_bytes(before)
        with patch.object(q.os, "kill", return_value=None), patch.object(self.host, "sys") as stop:
            with self.assertRaises(q.MigrationError):
                with q.preserve_stops(self.host, {self.old.port}):
                    self.fail("foreign stop protection was ignored")
            self.assertEqual(marker.read_bytes(), before)
            with self.assertRaises(q.MigrationError):
                q.stop_defined(self.host, {self.old.unit})
            self.assertEqual(marker.read_bytes(), before)
            stop.assert_not_called()

    def test_old_cleanup_is_waited_out_with_preserve_marker_already_installed(self):
        marker = Path(q.LOCK_DIR) / f"stoppost-preserve.{self.old.tag}"
        state = {"cleaning": True}
        original = self.host.property
        def property_value(unit, name):
            if unit == self.old.unit and name == "ControlPID":
                return "1234" if state["cleaning"] else "0"
            return original(unit, name)
        def cleanup_finishes(_seconds):
            self.assertEqual(marker.read_text(), f"{os.getpid()}\n")
            state["cleaning"] = False
        with patch.object(self.host, "property", side_effect=property_value), \
                patch.object(q.time, "sleep", side_effect=cleanup_finishes) as sleep:
            with q.preserve_stops(self.host, {self.old.port}):
                self.assertFalse(state["cleaning"])
                self.assertEqual(marker.read_text(), f"{os.getpid()}\n")
            sleep.assert_called_once()
        self.assertFalse(marker.exists())

    def test_source_daily_exports_saved_sample_without_collecting_or_writing(self):
        program = self.host.work / "traffic.py"
        program.write_bytes(b"fixture program")
        path = Path(q.STATE) / "traffic/daily.json"
        path.parent.mkdir()
        record = dict(tag=self.old.tag, port=self.old.port, key=q.daily_key(self.old),
                      days={"2026-11-17": {"upload": 1234, "download": 5678}},
                      sampled_at="2026-11-17T12:00:00+08:00", last={"u": {"bytes": 1234}})
        state = dict(schema=1, users={self.old.tag: record, "unselected": {"fixture": True}})
        path.write_bytes(q.encode_json(state))
        before = path.read_bytes(), path.stat().st_mtime_ns
        module = SimpleNamespace(load_state=Mock(side_effect=lambda source: q.decode_json(Path(source).read_bytes())),
                                 collect=Mock(side_effect=AssertionError("must not collect source traffic")))
        with patch.object(q, "TRAFFIC_PROGRAM", str(program)), \
                patch.object(self.host, "daily_module", return_value=module), \
                patch.object(self.host, "collect_daily", side_effect=AssertionError("must not collect source traffic")), \
                patch.object(self.host, "run", side_effect=AssertionError("must not invoke source commands")), \
                patch.object(q, "write_file", side_effect=AssertionError("must not rewrite source state")):
            result = q.Host.source_daily(self.host, [self.old])
        module.load_state.assert_called_once_with(path)
        module.collect.assert_not_called()
        self.assertEqual(result, {"program_sha256": q.TRAFFIC_SHA256,
                                  "users": {self.old.tag: q.portable_daily(record)}})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertNotIn("last", result["users"][self.old.tag])

    def test_source_daily_missing_sample_fails_without_initializing_counters(self):
        program = self.host.work / "traffic.py"
        program.write_bytes(b"fixture program")
        module = SimpleNamespace(collect=Mock(side_effect=AssertionError("must not initialize source counters")))
        with patch.object(q, "TRAFFIC_PROGRAM", str(program)), \
                patch.object(self.host, "daily_module", return_value=module), \
                patch.object(self.host, "collect_daily", side_effect=AssertionError("must not initialize source counters")):
            with self.assertRaisesRegex(q.MigrationError, "尚未采样"):
                q.Host.source_daily(self.host, [self.old])
        module.collect.assert_not_called()
        self.assertFalse(q.daily_path().exists())


class ValidationTests(unittest.TestCase):
    def test_standard_main_temp_and_nat_configs(self):
        for value in (node("main", 443), node("hy2-temp-local", 40000, quota=True, iplimit=True),
                      node("hy2-temp-nat", 40001, nat=True)):
            self.assertIs(q.validate_node(value), value)

    def test_modified_yaml_is_rejected(self):
        value = node("main", 443)
        value.config += "resolver:\n  type: https\n  https:\n    addr: https://untrusted.example/dns-query\n"
        with self.assertRaises(q.MigrationError):
            q.validate_node(value)

    def test_metadata_newlines_and_shell_values_rejected(self):
        for text in ("a\nPORT=22", "a\rPORT=22"):
            value = node("main", 443)
            value.meta["MAIN_PASSWORD"] = text
            with self.assertRaises(q.MigrationError):
                q.validate_node(value)

    def test_link_credential_mismatch_rejected(self):
        value = node("main", 443)
        value.url = value.url.replace("source-443-fixture-password", "incorrect-password")
        with self.assertRaises(q.MigrationError):
            q.validate_node(value)

    def test_corrupt_quota_cannot_reset_usage(self):
        value = node("hy2-temp-quota", 40000, quota=True)
        value.quota["LIMIT_BYTES"] = "100"
        with self.assertRaises(q.MigrationError):
            q.validate_node(value)

    def test_duplicate_ports_and_main_collision_rejected(self):
        for values in ([node("main", 443), node("hy2-temp-new", 443)],
                       [node("main", 443), node("hy2-temp-a", 40000), node("hy2-temp-b", 40000)]):
            with self.assertRaises(q.MigrationError):
                q.decode_bundle(packet(values))

    def test_foreign_project_packet_rejected(self):
        raw = q.decode_json(packet([node("main", 443)]))
        raw["format"] = 1 if q.FORMAT != 1 else "vless"
        with self.assertRaises(q.MigrationError):
            q.decode_bundle(q.encode_json(raw))

    def test_udp_fence_uses_only_selected_ports(self):
        host = q.Host(Path("/run"))
        with patch.object(host, "nft") as nft:
            host.fence("hy2qianyi_0123456789abcdef", {443, 40000})
        batch = nft.call_args.args[0]
        self.assertIn("udp dport", batch)
        self.assertIn("udp sport", batch)
        self.assertIn("443, 40000", batch)
        self.assertNotIn("flush ruleset", batch)

    def test_bundle_does_not_include_wireguard_private_key(self):
        raw = q.decode_json(packet([node("main", 443), node("hy2-temp-nat", 40000, nat=True)]))
        self.assertEqual(set(raw), {"format", "origin", "profile", "nodes", "blacklist", "daily"})
        self.assertNotIn("PrivateKey", q.encode_json(raw).decode())
        self.assertNotIn("/etc/wireguard/", q.encode_json(raw).decode())

class NativeAdapterTests(unittest.TestCase):
    def nat_host(self, *, handshake=None, wrong_peer=False, prohibit=True):
        host = q.Host(Path("/run"))
        now = int(time.time())
        handshake = now if handshake is None else handshake
        def run(*args, **kwargs):
            if args[:5] == ("ip", "-j", "-4", "route", "show"):
                routes = [{"dst": "default", "dev": "wg-nat"}]
                if prohibit:
                    routes.append({"dst": "default", "type": "prohibit"})
                text = q.encode_json(routes).decode()
            elif args[:5] == ("ip", "-j", "-4", "rule", "show"):
                text = '[{"oif":"wg-nat","table":200,"priority":31000}]'
            elif args[:5] == ("ip", "-j", "-4", "route", "get"):
                text = '[{"dev":"wg-nat"}]'
            elif args == ("wg", "show", "wg-nat", "allowed-ips"):
                text = "default-peer\t0.0.0.0/0\nother-peer\t10.0.0.0/24\n"
            elif args == ("wg", "show", "wg-nat", "latest-handshakes"):
                default_stamp = 0 if wrong_peer else handshake
                text = f"default-peer\t{default_stamp}\nother-peer\t{now}\n"
            else:
                raise AssertionError(args)
            return SimpleNamespace(returncode=0, stdout=text, stderr="")
        host.run = run
        host.active = lambda unit: True
        host.enabled = lambda unit: True
        host.inventory = lambda: {}
        return host

    def test_nat_rebinds_to_target_route_without_copying_peer_keys(self):
        host = self.nat_host()
        incoming = node("hy2-temp-nat", 40000, nat=True)
        with patch.object(q, "secure_read", return_value=b"MARK=8080\nTABLE_ID=200\nRULE_PRIORITY=31001\n"):
            host.adapt_nat(incoming)
        self.assertEqual(incoming.meta["MARK"], "8080")
        self.assertEqual(incoming.meta["TABLE_ID"], "200")
        self.assertEqual(incoming.meta["OIF_RULE_PRIORITY"], "31000")

    def test_nat_requires_recent_handshake_from_default_peer(self):
        cases = [dict(handshake=int(time.time()) - 1000), dict(handshake=int(time.time()) + 1000),
                 dict(wrong_peer=True)]
        for case in cases:
            with self.subTest(case=case), patch.object(q, "secure_read", return_value=b"MARK=8080\nTABLE_ID=200\nRULE_PRIORITY=31001\n"):
                with self.assertRaises(q.MigrationError):
                    self.nat_host(**case).adapt_nat(node("hy2-temp-nat", 40000, nat=True))

    def test_nat_rejects_missing_fail_closed_route(self):
        with patch.object(q, "secure_read", return_value=b"MARK=8080\nTABLE_ID=200\nRULE_PRIORITY=31001\n"):
            with self.assertRaises(q.MigrationError):
                self.nat_host(prohibit=False).adapt_nat(node("hy2-temp-nat", 40000, nat=True))

    def test_target_tcp_service_on_selected_udp_port_is_rejected(self):
        host = q.Host(Path("/run"))
        with patch.object(host, "run", return_value=SimpleNamespace(stdout='LISTEN 0 128 0.0.0.0:40000 0.0.0.0:* users:(("sshd",pid=10,fd=3))\n')):
            with self.assertRaises(q.MigrationError):
                host.listeners({40000})


class GuardDataTests(unittest.TestCase):
    def test_prepared_blacklist_plan_is_usable_before_journal_serialization(self):
        # Production applies this exact in-memory plan before reading its JSON
        # journal; a JSON round-trip here would conceal tuple/list mismatches.
        previous = q.encode_json(dict(schema=1, paused=[45000], bans={"45000": ["192.0.2.0/24"]}))
        requested = {(40002, "198.51.100.0/24"), (40003, "2001:db8::/64")}
        host = q.Host(Path("/unused-migration-test"))
        with patch.object(host, "guard_verify", return_value=previous), \
                patch.object(q, "exists", return_value=False):
            plan = host.prepare_blacklist(requested)
        original, before, additions = q.ban_plan_state(plan, {443, 40002, 40003})
        self.assertEqual(original, previous)
        self.assertEqual(before, {(45000, "192.0.2.0/24")})
        self.assertEqual(additions, requested)
        state, merged = q.guard_state(q.merged_guard(original, additions))
        self.assertEqual(state["paused"], [45000])
        self.assertEqual(merged, before | requested)

    def test_merging_bans_keeps_other_ports_pauses_and_ipv6(self):
        before = q.encode_json(dict(schema=1, paused=[40001], bans={"40001": ["192.0.2.0/24"]}))
        merged = q.merged_guard(before, {(40000, "2001:db8::/32"), (443, "198.51.100.5/32")})
        state, entries = q.guard_state(merged)
        self.assertEqual(state["paused"], [40001])
        self.assertEqual(entries, {(40001, "192.0.2.0/24"), (40000, "2001:db8::/32"), (443, "198.51.100.5/32")})

    def test_noncanonical_network_and_foreign_port_are_rejected(self):
        with self.assertRaises((q.MigrationError, ValueError)):
            q.ban_entry(443, "192.0.2.1/24")
        with self.assertRaises(q.MigrationError):
            q.ban_entries([[22, "192.0.2.1/32"]], {443})


class SourceEndpointTests(unittest.TestCase):
    def settings(self, connection, argv=(), replies=None):
        with patch.dict(os.environ, {"SSH_CONNECTION": connection}), \
                patch.object(q.sys.stdin, "isatty", return_value=replies is not None), \
                patch("builtins.input", side_effect=replies) as prompt:
            value = q.source_endpoint(q.parse_args(["send", *argv]))
            return value, prompt.call_args_list

    def test_detected_custom_ssh_port_is_default_and_transport_is_ephemeral(self):
        result, prompts = self.settings("192.0.2.1 50000 8.8.8.8 22022", replies=["", ""])
        self.assertEqual(result, ("8.8.8.8", 22022, 0))
        self.assertIn("22022", prompts[1].args[0])

    def test_private_server_address_and_external_mapping_can_be_entered(self):
        result, prompts = self.settings("192.0.2.1 50000 10.0.0.5 22", replies=["203.0.113.10", "2222"])
        self.assertEqual(result, ("203.0.113.10", 2222, 0))
        self.assertNotIn("10.0.0.5", prompts[0].args[0])

    def test_console_uses_22_when_no_ssh_session(self):
        result, prompts = self.settings("", replies=["203.0.113.10", ""])
        self.assertEqual(result, ("203.0.113.10", 22, 0))
        self.assertIn("22", prompts[1].args[0])

    def test_mapped_ipv4_and_noninteractive_cli(self):
        result, prompts = self.settings("192.0.2.1 50000 ::ffff:8.8.8.8 22022")
        self.assertEqual(result, ("8.8.8.8", 22022, 0))
        self.assertEqual(prompts, [])

    def test_explicit_overrides_and_direct_mode_remain_supported(self):
        login = "192.0.2.1 50000 8.8.8.8 22022"
        explicit, prompts = self.settings(login, ["--host", "203.0.113.10", "--ssh-port", "22",
                                                 "--port", "49000"], replies=[])
        self.assertEqual(explicit, ("203.0.113.10", 22, 49000))
        self.assertEqual(prompts, [])
        direct, prompts = self.settings(login, ["--direct"], replies=[""])
        self.assertEqual(direct, ("8.8.8.8", 22022, 48999))
        self.assertEqual(len(prompts), 1)


class SshFrameTests(unittest.TestCase):
    def test_binary_snapshot_round_trips_after_a_bounded_banner(self):
        snapshot = bytes(range(256)) * 1024
        output = BytesIO(b"ssh banner\n")
        output.seek(0, os.SEEK_END)
        q.write_ssh_frame(output, snapshot)
        self.assertEqual(q.read_ssh_frame(BytesIO(output.getvalue())), snapshot)

    def test_truncated_modified_or_extended_frames_are_rejected(self):
        output = BytesIO()
        q.write_ssh_frame(output, b"secret snapshot")
        frame = output.getvalue()
        for invalid in (frame[:-1], frame[:-1] + b"z", frame + b"unexpected",
                        b"x" * q.MAX_SSH_PREAMBLE + b"\n" + frame):
            with self.subTest(frame=invalid[:12]), self.assertRaises(q.MigrationError):
                q.read_ssh_frame(BytesIO(invalid))

    def test_source_workspace_is_cleaned_before_emitting_a_frame(self):
        events = []
        class Output(BytesIO):
            def write(self, data):
                self_test.assertEqual(events, ["legacy checked", "legacy cleaned", "workspace removed"])
                return super().write(data)

        self_test = self
        stream = Output()
        output = SimpleNamespace(isatty=lambda: False, buffer=stream)
        host = SimpleNamespace(legacy=lambda remove=False: events.append("legacy cleaned" if remove else "legacy checked"))
        workspace = SimpleNamespace(cleanup=lambda: events.append("workspace removed"))
        with patch.object(q.sys, "stdout", output), patch.object(q, "source_bundle", return_value=b"snapshot"):
            q.export_ssh(host, workspace)
        self.assertEqual(q.read_ssh_frame(BytesIO(stream.getvalue())), b"snapshot")

    def test_interactive_export_does_not_print_a_snapshot(self):
        with patch.object(q.sys.stdout, "isatty", return_value=True), self.assertRaises(q.MigrationError):
            q.export_ssh(None, None)


class TransportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
    def test_pinned_transfer_proves_source_workspace_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "source"
            work.mkdir()
            workspace = q.Workspace(work)
            context, pin = q.ssl_server(q.Host(work), work)
            body = b"test snapshot"
            token = "a" * 43
            ready, done = queue.Queue(), queue.Queue()
            def serve():
                try:
                    q.serve_packet("127.0.0.1", 0, context, body, token, 10, ready.put, workspace.cleanup)
                    done.put(None)
                except Exception as error:
                    done.put(error)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            port = ready.get(timeout=5)
            with socket.create_connection(("127.0.0.1", port)) as raw:
                with self.assertRaisesRegex(q.MigrationError, "证书指纹不符"):
                    q.fetch_packet(raw, "0" * 64, token, q.sha(body))
            self.assertTrue(work.exists())
            with socket.create_connection(("127.0.0.1", port)) as raw:
                with self.assertRaises(OSError):
                    q.fetch_packet(raw, pin, "b" * 43, q.sha(body))
            self.assertTrue(work.exists())
            with socket.create_connection(("127.0.0.1", port)) as raw:
                self.assertEqual(q.fetch_packet(raw, pin, token, q.sha(body)), body)
                self.assertFalse(work.exists())
            thread.join(timeout=5)
            self.assertIsNone(done.get(timeout=1))
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", port), timeout=1)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
    def test_tls_over_unix_socket_like_ssh_forwarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            context, pin = q.ssl_server(q.Host(work), work)
            token = "a" * 43
            source, target = socket.socketpair()
            failures = queue.Queue()
            def serve():
                try:
                    with context.wrap_socket(source, server_side=True) as conn:
                        if not q.check_request(conn, token, time.monotonic() + 3):
                            raise AssertionError("unexpected authorization")
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx")
                except Exception as error:
                    failures.put(error)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            with target:
                self.assertEqual(q.fetch_packet(target, pin, token, q.sha(b"x")), b"x")
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(failures.empty())

    def test_bootstrap_checks_hash_and_cleans_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = b"import pathlib,sys\npathlib.Path(sys.argv[1]).write_text('ran')\n"
            (root / "sample").write_bytes(sample)
            (root / "curl").write_text(f"#!/bin/sh\nfor out; do :; done\ncp {shlex.quote(str(root / 'sample'))} \"$out\"\n")
            (root / "curl").chmod(0o755)
            environment = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}")
            script = q.BOOTSTRAP.replace("/run/hy2qianyi.XXXXXXXX", str(root / "hy2qianyi.XXXXXXXX"))
            for digest, expected in (("0" * 64, False), (q.sha(sample), True)):
                result = subprocess.run(["bash", "-c", script, "hy2qianyi", q.SOURCE_URL, digest, str(root / "flag")],
                                        capture_output=True, env=environment)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
                self.assertEqual((root / "flag").exists(), expected)
                self.assertFalse(list(root.glob("hy2qianyi.*")))


class ExportCleanupTests(unittest.TestCase):
    def test_export_without_guard_does_not_create_runtime_lock(self):
        host = q.Host(Path("/unused"))
        with patch.object(q, "exists", return_value=False), \
                patch.object(host, "blacklist_lock", side_effect=AssertionError("must not create a lock")), \
                patch.object(host, "guard_verify", return_value=None):
            self.assertEqual(host.source_blacklist({443}), set())

    def test_guard_state_without_native_lock_is_rejected_read_only(self):
        host = q.Host(Path("/unused"))
        with patch.object(q, "exists", side_effect=lambda p: p == q.GUARD_STATE), \
                patch.object(host, "blacklist_lock", side_effect=AssertionError("must not create a lock")):
            with self.assertRaises(q.MigrationError):
                host.source_blacklist({443})


if __name__ == "__main__":
    unittest.main()
