"""
Adversarial Concurrency & Integration Stress Verification Suite (Milestone 1).
Author: m1_challenger_2 (Adversarial Code-Executing Verifier)

Empirically stress-tests:
1. Concurrency of get_config() and PanelConfig.load() across 50 threads with barrier synchronization.
2. Rapid concurrent disk modifications to .panel_settings.json while 50 threads continuously read properties.
3. Thread-safety and path synchronization of server.set_data_dir() under multi-threaded load.
4. PBKDF2 hashing in access.py under concurrent authentications with custom PBKDF2_ITER.
5. multi.py loopback worker subprocess spawning with --port 0 binding distinct ephemeral ports without collision or deadlock.
"""

import concurrent.futures
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from config import PanelConfig, get_config, set_config, ConfigError
import access
from access import PanelAccess, AccessError
import server


# ============================================================================
# 1. CONCURRENT get_config() AND PanelConfig.load() (50 WORKER THREADS)
# ============================================================================

class TestConcurrentGetConfigAndLoad:
    """Stress-test thread safety of global singleton and config instantiation under high concurrency."""

    def test_concurrent_get_config_from_uninitialized_singleton(self):
        """50 worker threads concurrently call get_config() when _GLOBAL_CONFIG is None."""
        config.set_config(None)  # Reset singleton to uninitialized state
        assert config._GLOBAL_CONFIG is None

        barrier = threading.Barrier(50)
        results = [None] * 50
        exceptions = [None] * 50

        def worker(thread_idx: int):
            try:
                barrier.wait(timeout=10.0)
                cfg = config.get_config()
                results[thread_idx] = cfg
            except Exception as e:
                exceptions[thread_idx] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        elapsed = time.perf_counter() - t0

        # Assert no exceptions occurred
        for i, exc in enumerate(exceptions):
            assert exc is None, f"Thread {i} raised exception: {exc}"

        # Assert every thread received a valid, fully formed PanelConfig
        for i, res in enumerate(results):
            assert res is not None, f"Thread {i} got None"
            assert isinstance(res, PanelConfig), f"Thread {i} got unexpected type {type(res)}"
            assert hasattr(res, "server") and hasattr(res, "device") and hasattr(res, "glagol")
            assert hasattr(res, "security") and hasattr(res, "remote") and hasattr(res, "party")
            assert hasattr(res, "assistant")
            assert res.server.port == 8765
            assert res.server.host == "127.0.0.1"

        print(f"\n[STRESS 1.1] 50 threads concurrent get_config(): {elapsed:.4f}s")

    def test_concurrent_load_and_mutation_isolation(self):
        """50 worker threads concurrently load isolated configurations with distinct overrides."""
        with tempfile.TemporaryDirectory() as td:
            base_dir = Path(td)
            barrier = threading.Barrier(50)
            results = [None] * 50
            exceptions = [None] * 50

            def worker(thread_idx: int):
                try:
                    barrier.wait(timeout=10.0)
                    custom_env = {
                        "PANEL_PORT": str(10000 + thread_idx),
                        "PANEL_HOST": f"192.168.1.{thread_idx + 1}",
                        "ADB_SERIAL": f"device_{thread_idx}:5555",
                    }
                    cfg = PanelConfig.load(settings_path=base_dir, env=custom_env, strict=True)
                    results[thread_idx] = cfg
                except Exception as e:
                    exceptions[thread_idx] = e

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
            t0 = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            elapsed = time.perf_counter() - t0

            for i, exc in enumerate(exceptions):
                assert exc is None, f"Thread {i} raised exception: {exc}"

            for i, cfg in enumerate(results):
                assert cfg is not None
                assert cfg.server.port == 10000 + i
                assert cfg.server.host == f"192.168.1.{i + 1}"
                assert cfg.device.serial == f"device_{i}:5555"

            print(f"[STRESS 1.2] 50 threads concurrent PanelConfig.load(): {elapsed:.4f}s")

    def test_interleaved_get_set_config(self):
        """Interleaved concurrent get_config() and set_config() calls."""
        stop_event = threading.Event()
        errors = []

        def setter_worker():
            idx = 0
            while not stop_event.is_set():
                new_cfg = PanelConfig()
                new_cfg.server.port = 8000 + (idx % 100)
                config.set_config(new_cfg)
                idx += 1
                time.sleep(0.0001)

        def getter_worker(worker_id: int):
            for _ in range(100):
                cfg = config.get_config()
                if cfg is None or not isinstance(cfg, PanelConfig):
                    errors.append(f"Worker {worker_id}: Invalid config retrieved: {cfg}")
                elif cfg.server.port < 8000 and cfg.server.port != 8765:
                    errors.append(f"Worker {worker_id}: Corrupted port: {cfg.server.port}")
                time.sleep(0.0001)

        setter_threads = [threading.Thread(target=setter_worker) for _ in range(5)]
        getter_threads = [threading.Thread(target=getter_worker, args=(i,)) for i in range(45)]

        t0 = time.perf_counter()
        for t in setter_threads + getter_threads:
            t.start()
        for t in getter_threads:
            t.join(timeout=10.0)
        stop_event.set()
        for t in setter_threads:
            t.join(timeout=10.0)
        elapsed = time.perf_counter() - t0

        assert not errors, f"Errors during interleaved get/set: {errors[:5]}"
        print(f"[STRESS 1.3] 50 threads interleaved get/set config: {elapsed:.4f}s")


# ============================================================================
# 2. RAPID CONCURRENT DISK MODIFICATIONS TO .panel_settings.json
# ============================================================================

class TestConcurrentSettingsFileModifications:
    """Stress-test configuration loading while .panel_settings.json is being mutated on disk."""

    def test_rapid_concurrent_atomic_updates_vs_reads(self):
        """50 reader threads continuously load config while a writer rapidly updates .panel_settings.json atomically."""
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            settings_file = data_dir / ".panel_settings.json"
            stop_event = threading.Event()
            read_errors = []
            successful_reads = [0] * 50

            # Pre-populate settings file
            settings_file.write_text(json.dumps({"server": {"port": 9000}}), encoding="utf-8")

            def writer_worker():
                counter = 0
                while not stop_event.is_set():
                    counter += 1
                    port_val = 9000 + (counter % 500)
                    tmp_file = data_dir / f".panel_settings.tmp.{counter}"
                    tmp_file.write_text(json.dumps({"server": {"port": port_val}, "device": {"serial": f"dev_{counter}"}}), encoding="utf-8")
                    try:
                        os.replace(tmp_file, settings_file)
                    except OSError:
                        pass
                    time.sleep(0.0005)

            def reader_worker(reader_idx: int):
                for _ in range(20):
                    try:
                        cfg = PanelConfig.load(settings_path=data_dir, strict=False)
                        if cfg is None or not isinstance(cfg, PanelConfig):
                            read_errors.append(f"Reader {reader_idx}: None or invalid config")
                        elif not (9000 <= cfg.server.port <= 9500 or cfg.server.port == 8765):
                            read_errors.append(f"Reader {reader_idx}: invalid port {cfg.server.port}")
                        else:
                            successful_reads[reader_idx] += 1
                    except Exception as e:
                        read_errors.append(f"Reader {reader_idx} exception: {e}")
                    time.sleep(0.001)

            writer = threading.Thread(target=writer_worker)
            readers = [threading.Thread(target=reader_worker, args=(i,)) for i in range(50)]

            t0 = time.perf_counter()
            writer.start()
            for r in readers:
                r.start()
            for r in readers:
                r.join(timeout=10.0)
            stop_event.set()
            writer.join(timeout=10.0)
            elapsed = time.perf_counter() - t0

            total_success = sum(successful_reads)
            assert not read_errors, f"Read errors encountered: {read_errors[:5]}"
            assert total_success == 1000, f"Expected 1000 reads, got {total_success}"
            print(f"[STRESS 2.1] Rapid atomic writes vs 50 readers (1000 reads): {elapsed:.4f}s, errors=0")

    def test_non_atomic_file_truncation_resilience_in_readers(self):
        """Simulate raw file truncation / non-atomic writes: readers with strict=False must never crash."""
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            settings_file = data_dir / ".panel_settings.json"
            stop_event = threading.Event()
            crashes = []

            settings_file.write_text(json.dumps({"server": {"port": 7000}}), encoding="utf-8")

            def corrupting_writer():
                while not stop_event.is_set():
                    # Intentionally write partial or corrupt JSON directly without atomic rename
                    try:
                        with open(settings_file, "w", encoding="utf-8") as f:
                            f.write("{\"server\": ")
                            f.flush()
                            time.sleep(0.0002)
                            f.write("{\"port\": 7777}}")
                    except OSError:
                        pass
                    time.sleep(0.001)

            def resilient_reader(idx: int):
                for _ in range(25):
                    try:
                        cfg = PanelConfig.load(settings_path=data_dir, strict=False)
                        # Must return valid PanelConfig even if JSON parse failed (falling back to defaults)
                        assert isinstance(cfg, PanelConfig)
                        assert cfg.server.port in (7777, 8765)  # Either successfully parsed 7777 or fell back to default 8765
                    except Exception as e:
                        crashes.append(f"Reader {idx} crashed: {e}")

            writer = threading.Thread(target=corrupting_writer)
            readers = [threading.Thread(target=resilient_reader, args=(i,)) for i in range(40)]

            t0 = time.perf_counter()
            writer.start()
            for r in readers:
                r.start()
            for r in readers:
                r.join(timeout=10.0)
            stop_event.set()
            writer.join(timeout=10.0)
            elapsed = time.perf_counter() - t0

            assert not crashes, f"Readers crashed during raw file writes: {crashes[:5]}"
            print(f"[STRESS 2.2] Non-atomic write fuzzing against 40 readers (1000 reads): {elapsed:.4f}s, crashes=0")


# ============================================================================
# 3. server.set_data_dir() MULTI-THREADED SYNCHRONIZATION
# ============================================================================

class TestSetDataDirMultiThreadedSync:
    """Stress-test synchronization of server.DATA_DIR, server.AUTH_FILE, and config.py under multi-threaded load."""

    def test_concurrent_set_data_dir_synchronization(self):
        """Concurrently call set_data_dir() across 50 threads and verify module-level synchronization."""
        original_data_dir = server.DATA_DIR

        with tempfile.TemporaryDirectory() as td:
            base_dir = Path(td)
            dirs = [base_dir / f"session_{i}" for i in range(50)]
            for d in dirs:
                d.mkdir(parents=True, exist_ok=True)

            barrier = threading.Barrier(50)
            sync_errors = []

            def worker(idx: int):
                target_dir = dirs[idx]
                try:
                    barrier.wait(timeout=10.0)
                    server.set_data_dir(target_dir)

                    # Inspect immediate module consistency
                    # DATA_DIR must match the Path resolved
                    with getattr(server, "DATA_DIR_LOCK", threading.Lock()):
                        cur_data_dir = server.DATA_DIR
                        cur_auth = server.AUTH_FILE
                        cur_panel_auth = server.PANEL_AUTH_FILE
                        cur_metrics = server.METRICS_FILE
                        cur_tls = server.TLS_DIR
                        cur_settings = server.SETTINGS_FILE
                        cur_agent = server.AGENT_FILE

                    # Internal consistency check: all module files must reside within cur_data_dir
                    if cur_auth != cur_data_dir / ".yandex_auth.json":
                        sync_errors.append(f"Thread {idx}: AUTH_FILE mismatch: {cur_auth} != {cur_data_dir}")
                    if cur_panel_auth != cur_data_dir / ".panel_auth.json":
                        sync_errors.append(f"Thread {idx}: PANEL_AUTH_FILE mismatch: {cur_panel_auth} != {cur_data_dir}")
                    if cur_metrics != cur_data_dir / ".metrics.json":
                        sync_errors.append(f"Thread {idx}: METRICS_FILE mismatch: {cur_metrics} != {cur_data_dir}")
                    if cur_tls != cur_data_dir / ".tls":
                        sync_errors.append(f"Thread {idx}: TLS_DIR mismatch: {cur_tls} != {cur_data_dir}")
                    if cur_settings != cur_data_dir / ".panel_settings.json":
                        sync_errors.append(f"Thread {idx}: SETTINGS_FILE mismatch: {cur_settings} != {cur_data_dir}")
                    if cur_agent != cur_data_dir / ".agent.json":
                        sync_errors.append(f"Thread {idx}: AGENT_FILE mismatch: {cur_agent} != {cur_data_dir}")

                    # Check config singleton synchronization
                    cfg = config.get_config()
                    if cfg.server.data_dir != cur_data_dir:
                        # Under high multi-threaded concurrency without lock, DATA_DIR can be updated by another thread,
                        # but cfg.server.data_dir must always be one of the valid target directories
                        if cfg.server.data_dir not in dirs:
                            sync_errors.append(f"Thread {idx}: cfg.server.data_dir corrupt: {cfg.server.data_dir}")
                except Exception as e:
                    sync_errors.append(f"Thread {idx} exception: {e}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
            t0 = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            elapsed = time.perf_counter() - t0

            # Restore original data_dir
            server.set_data_dir(original_data_dir)

            assert not sync_errors, f"Synchronization errors in set_data_dir: {sync_errors[:5]}"
            print(f"[STRESS 3.1] 50 threads concurrent set_data_dir(): {elapsed:.4f}s, errors=0")


# ============================================================================
# 4. access.py PBKDF2 HASHING UNDER CONCURRENT AUTHENTICATION
# ============================================================================

class TestPBKDF2ConcurrentAuthentication:
    """Stress-test PBKDF2 authentication, rate limiting, and thread safety in access.py."""

    def test_concurrent_logins_with_custom_pbkdf2_iter(self):
        """50 concurrent threads authenticating with custom PBKDF2_ITER configured."""
        with tempfile.TemporaryDirectory() as td:
            auth_file = Path(td) / ".panel_auth.json"

            # Test with custom PBKDF2 iterations (e.g., 5,000 for responsive stress testing)
            custom_iter = 5000
            access.PBKDF2_ITER = custom_iter

            panel_access = PanelAccess(auth_file)
            initial_token = panel_access.set_password("", "SecureMasterKey2026!", "setup-agent")

            assert panel_access.enabled is True
            assert panel_access.data.get("iter") == custom_iter
            assert auth_file.exists()

            barrier = threading.Barrier(50)
            login_tokens = [None] * 25
            invalid_rejections = [None] * 25
            concurrency_errors = []

            def valid_login_worker(worker_idx: int):
                ip = f"192.168.1.{worker_idx + 1}"
                try:
                    barrier.wait(timeout=10.0)
                    token = panel_access.login(ip, "SecureMasterKey2026!", f"agent-{worker_idx}")
                    login_tokens[worker_idx] = token
                except Exception as e:
                    concurrency_errors.append(f"Valid worker {worker_idx} failed: {e}")

            def invalid_login_worker(worker_idx: int):
                ip = f"10.0.0.{worker_idx + 1}"
                try:
                    barrier.wait(timeout=10.0)
                    panel_access.login(ip, "WRONG_PASSWORD_XYZ", f"attacker-{worker_idx}")
                    concurrency_errors.append(f"Invalid worker {worker_idx} unexpectedly succeeded!")
                except AccessError as e:
                    invalid_rejections[worker_idx] = str(e)
                except Exception as e:
                    concurrency_errors.append(f"Invalid worker {worker_idx} raised non-AccessError: {e}")

            threads = []
            for i in range(25):
                threads.append(threading.Thread(target=valid_login_worker, args=(i,)))
                threads.append(threading.Thread(target=invalid_login_worker, args=(i,)))

            t0 = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
            elapsed = time.perf_counter() - t0

            assert not concurrency_errors, f"Concurrency errors during auth: {concurrency_errors[:5]}"

            # Verify all 25 valid logins succeeded with unique tokens
            assert all(t is not None for t in login_tokens)
            assert len(set(login_tokens)) == 25, "Session tokens must be distinct!"

            # Verify all 25 invalid logins were rejected with 'Неверный пароль'
            assert all(r is not None and "Неверный пароль" in r for r in invalid_rejections)

            # Verify session count: 1 (set_password) + 25 (logins) = 26 sessions
            assert len(panel_access.data.get("sessions", {})) == 26

            # Verify concurrent session_ok checks across all 50 threads
            verify_errors = []
            def session_checker(worker_idx: int):
                token = login_tokens[worker_idx % 25]
                if not panel_access.session_ok(token):
                    verify_errors.append(f"Checker {worker_idx}: session_ok failed for valid token")

            verify_threads = [threading.Thread(target=session_checker, args=(i,)) for i in range(50)]
            for t in verify_threads:
                t.start()
            for t in verify_threads:
                t.join(timeout=10.0)

            assert not verify_errors, f"Errors in concurrent session_ok: {verify_errors[:5]}"
            print(f"[STRESS 4.1] 50 concurrent auth requests (25 valid, 25 invalid) with iter={custom_iter}: {elapsed:.4f}s")

    def test_pbkdf2_backward_compatibility_when_iter_changes(self):
        """Verify that hashes created with iter=5000 continue to validate even if PBKDF2_ITER is changed to 100000."""
        with tempfile.TemporaryDirectory() as td:
            auth_file = Path(td) / ".panel_auth.json"

            access.PBKDF2_ITER = 5000
            pa = PanelAccess(auth_file)
            pa.set_password("", "SuperSecret123", "client-1")

            # Simulate configuration change increasing PBKDF2_ITER to 100000
            access.PBKDF2_ITER = 100000

            # Reload PanelAccess from disk
            pa2 = PanelAccess(auth_file)
            assert pa2.data.get("iter") == 5000

            # Login must use the hash's recorded iter (5000) and succeed
            token = pa2.login("127.0.0.1", "SuperSecret123", "client-2")
            assert token is not None
            assert pa2.session_ok(token) is True
            print(f"[STRESS 4.2] PBKDF2 backward compatibility across iteration upgrades: verified")


# ============================================================================
# 5. multi.py LOOPBACK WORKER SUBPROCESS SPAWNING WITH --port 0
# ============================================================================

class TestMultiEphemeralPortBinding:
    """Stress-test loopback worker subprocess spawning with --port 0 and distinct ephemeral port binding."""

    def test_concurrent_worker_subprocess_ephemeral_port_allocation(self):
        """Spawn 3 actual server.py loopback worker subprocesses with --port 0 and verify distinct port binding."""
        with tempfile.TemporaryDirectory() as td:
            base_dir = Path(td)
            gateway_secret = "test-secret-gateway-12345"

            workers_spec = [
                {"id": "spk_1", "model": "max", "device": "127.0.0.1:55551"},
                {"id": "spk_2", "model": "max", "device": "127.0.0.1:55552"},
                {"id": "spk_3", "model": "max", "device": "127.0.0.1:55553"},
            ]

            processes = []
            ready_files = []
            allocated_ports = []

            t0 = time.perf_counter()
            try:
                for spec in workers_spec:
                    worker_dir = base_dir / spec["id"]
                    worker_dir.mkdir(parents=True, exist_ok=True)
                    ready_file = base_dir / f"{spec['id']}_ready.json"
                    ready_files.append(ready_file)

                    env = {
                        **os.environ,
                        "PANEL_GATEWAY_SECRET": gateway_secret,
                        "PANEL_READY_FILE": str(ready_file),
                    }
                    cmd = [
                        sys.executable,
                        str(REPO_ROOT / "server.py"),
                        "--device", spec["device"],
                        "--model", spec["model"],
                        "--host", "127.0.0.1",
                        "--port", "0",
                        "--https-port", "0",
                        "--data-dir", str(worker_dir),
                    ]
                    log_file = open(worker_dir / "worker.log", "wb")
                    p = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)
                    processes.append((p, log_file))

                # Wait for all workers to write their ready file
                deadline = time.monotonic() + 25.0
                for ready_file in ready_files:
                    while not ready_file.exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"Worker failed to write ready file {ready_file} within timeout")
                        time.sleep(0.05)
                    data = json.loads(ready_file.read_text(encoding="utf-8"))
                    allocated_ports.append(data["port"])

                elapsed = time.perf_counter() - t0

                # Verify allocated ports
                print(f"\n[STRESS 5.1] Allocated ephemeral ports: {allocated_ports} in {elapsed:.4f}s")
                assert len(allocated_ports) == 3
                assert all(isinstance(p, int) for p in allocated_ports)
                assert all(1024 <= p <= 65535 for p in allocated_ports)
                assert len(set(allocated_ports)) == 3, f"Port collision detected! {allocated_ports}"

                # Test HTTP connectivity and security isolation on each worker's ephemeral port
                for port in allocated_ports:
                    # 1. Request WITHOUT X-Panel-Gateway secret must be blocked with 403 Forbidden
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        conn.request("GET", "/api/panel-auth")
                        resp = conn.getresponse()
                        body = resp.read()
                        assert resp.status == 403, f"Expected 403 without gateway secret on port {port}, got {resp.status}"
                        err_json = json.loads(body.decode("utf-8"))
                        assert err_json.get("error") == "private worker"
                    finally:
                        conn.close()

                    # 2. Request WITH X-Panel-Gateway secret must be allowed through
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        conn.request("GET", "/api/panel-auth", headers={"X-Panel-Gateway": gateway_secret})
                        resp = conn.getresponse()
                        body = resp.read()
                        assert resp.status == 200, f"Expected 200 with gateway secret on port {port}, got {resp.status}"
                        data = json.loads(body.decode("utf-8"))
                        assert "enabled" in data
                        assert "authed" in data
                    finally:
                        conn.close()

            finally:
                # Clean up processes
                for p, log_file in processes:
                    try:
                        p.terminate()
                        p.wait(timeout=5.0)
                    except Exception:
                        try:
                            p.kill()
                            p.wait(timeout=2.0)
                        except Exception:
                            pass
                    finally:
                        log_file.close()

            print(f"[STRESS 5.1] All 3 workers spawned, bound distinct ports, responded to HTTP, and terminated cleanly.")
