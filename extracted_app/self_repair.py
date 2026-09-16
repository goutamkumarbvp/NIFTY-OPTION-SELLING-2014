from __future__ import annotations

"""V18 verified code self-repair engine.

Design goals:
- Diagnose runtime/build failures from structured evidence and source inspection.
- Create immutable source snapshots before any mutation.
- Apply only explicitly allow-listed repair recipes by default.
- Test candidate code in an isolated temporary workspace.
- Require a full test/compile gate before promotion.
- Automatically rollback a promoted repair when post-promotion verification fails.
- Never enable live trading, change risk policy, or execute broker orders.

An optional AI provider can propose a patch, but the policy/test gates remain
mandatory. The default provider is deterministic so the engine works offline.
"""

import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import platform
import shlex
import uuid
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional, Protocol
from safety_authority import assert_paper_only


PROTECTED_FILES = {"safety_authority.py", "security_verifier.py", "self_repair.py", "pass14_engine.py", "advanced_risk.py"}
LIVE_BLOCK_TERMS = {
    "LIVE_TRADING", "live_orders_enabled", "place_order", "order_place",
    "broker.order", "risk_limits", "DAILY_MAX_LOSS", "MAX_LOSS",
}


@dataclass(frozen=True)
class Diagnosis:
    incident_id: str
    category: str
    severity: str
    root_cause: str
    confidence: float
    evidence: list[str]
    affected_files: list[str]
    repairable: bool
    recommended_action: str


@dataclass(frozen=True)
class PatchProposal:
    incident_id: str
    patch_id: str
    strategy: str
    target_file: str
    description: str
    old_sha256: str
    new_sha256: str
    diff: str
    confidence: float
    safety_class: str


@dataclass(frozen=True)
class Verification:
    passed: bool
    compile_passed: bool
    tests_passed: bool
    security_passed: bool
    output: str
    duration_seconds: float


@dataclass(frozen=True)
class RepairResult:
    ok: bool
    status: str
    diagnosis: Diagnosis
    proposal: Optional[PatchProposal]
    verification: Optional[Verification]
    rollback_performed: bool
    promoted: bool
    message: str


class AIRepairProvider(Protocol):
    def propose(self, diagnosis: Diagnosis, source: str, target_file: str) -> Optional[str]: ...


class DeterministicRepairProvider:
    """Offline provider for conservative, known repair patterns.

    This intentionally does not pretend to be a general LLM/Codex. It handles
    high-confidence syntax/import repairs and can be extended with an AI provider.
    """

    def propose(self, diagnosis: Diagnosis, source: str, target_file: str) -> Optional[str]:
        if diagnosis.category == "MISSING_IMPORT":
            m = re.search(r"No module named ['\"]([^'\"]+)['\"]", "\n".join(diagnosis.evidence))
            if m and m.group(1) in {"typing_extensions", "dateutil"}:
                # Do not silently install packages. Only repair known stdlib-compatible
                # compatibility cases when an explicit mapping exists.
                return None
        return None


class OpenAIRepairProvider:
    """OpenAI Responses API repair provider.

    Network access is opt-in and credentialed via environment variables. The
    provider only proposes source text; CodeSelfRepairEngine remains the sole
    authority for sandbox verification, safety gates, promotion and rollback.
    """
    def __init__(self, env=None):
        import urllib.request, urllib.error
        self._urllib_request = urllib.request
        e=dict(env or os.environ)
        self.api_key=e.get("OPENAI_API_KEY", "").strip()
        self.enabled=e.get("REPAIR_LLM_ENABLED", "false").lower()=="true"
        self.base_url=e.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model=e.get("REPAIR_LLM_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol"
        self.timeout=max(5.0,min(float(e.get("REPAIR_LLM_TIMEOUT_SECONDS","45")),120.0))
        self.max_source_chars=max(5000,min(int(e.get("REPAIR_LLM_MAX_SOURCE_CHARS","60000")),120000))

    def _extract_text(self, obj):
        if isinstance(obj.get("output_text"), str):
            return obj["output_text"]
        out=[]
        for item in obj.get("output",[]) or []:
            for c in item.get("content",[]) or []:
                if c.get("type")=="output_text" and isinstance(c.get("text"),str): out.append(c["text"])
        return "\n".join(out)

    def propose(self, diagnosis: Diagnosis, source: str, target_file: str) -> Optional[str]:
        if not self.enabled or not self.api_key:
            return None
        if os.getenv("TRADING_MODE", "paper").lower() != "paper" or os.getenv("LIVE_TRADING", "false").lower()=="true" or os.getenv("LIVE_ORDERS_ENABLED", "false").lower()=="true":
            return None
        source=source[:self.max_source_chars]
        prompt=(
            "You are the bounded code-repair provider for a PAPER-ONLY trading terminal. "
            "Return ONLY the complete replacement Python source for the target file, with no markdown fences. "
            "Do not add, enable, or modify live trading, broker order placement, risk-limit overrides, secrets, credentials, or safety gates. "
            "Preserve public interfaces unless the diagnosis requires otherwise. Fix only the diagnosed defect. "
            "The caller will compile, test, security-scan, sandbox, and rollback your proposal; do not assume it will be accepted.\n\n"
            f"Target file: {target_file}\nDiagnosis: {asdict(diagnosis) if hasattr(diagnosis, '__dataclass_fields__') else str(diagnosis)}\n\nCurrent source:\n{source}"
        )
        payload={"model":self.model,"instructions":"Produce a minimal safe code repair. Output complete source only.","input":prompt,"store":False}
        req=self._urllib_request.Request(
            self.base_url+"/responses",
            data=json.dumps(payload,separators=(",",":" )).encode("utf-8"),
            headers={"Content-Type":"application/json","Authorization":"Bearer "+self.api_key,"X-V27-Repair":"paper-only-verified"},
            method="POST")
        try:
            with self._urllib_request.urlopen(req,timeout=self.timeout) as r:
                obj=json.loads(r.read(500000).decode("utf-8"))
            text=self._extract_text(obj).strip()
            if text.startswith("```"):
                text=re.sub(r"^```(?:python)?\s*", "", text, flags=re.I)
                text=re.sub(r"\s*```$", "", text)
            return text[:self.max_source_chars] if text else None
        except Exception:
            return None


class RepairPolicy:
    """Hard policy gate for code repair."""

    def __init__(self, root: Path, paper_only: bool = True, live_orders_enabled: bool = False,
                 require_tests: bool = True, allow_auto_promote: bool = False):
        self.root = root.resolve()
        self.paper_only = bool(paper_only)
        self.live_orders_enabled = bool(live_orders_enabled)
        self.require_tests = bool(require_tests)
        self.allow_auto_promote = bool(allow_auto_promote)

    def validate(self) -> tuple[bool, str]:
        # Auto-repair promotion is an environment capability, never a generic
        # permission. It is valid only inside an explicitly named paper runtime.
        if not self.paper_only or self.live_orders_enabled:
            return False, "CODE_REPAIR_REQUIRES_PAPER_ONLY"
        try:
            assert_paper_only()
        except Exception as e:
            return False, str(e)
        if os.getenv("LIVE_ORDERS_ENABLED", "false").lower() == "true":
            return False, "LIVE_ORDER_FLAG_BLOCKS_AUTO_REPAIR"
        return True, "OK"

    def patch_allowed(self, old: str, new: str, path: Path) -> tuple[bool, str]:
        rel = str(path.resolve().relative_to(self.root)) if path.resolve().is_relative_to(self.root) else "OUTSIDE_ROOT"
        if rel == "OUTSIDE_ROOT":
            return False, "TARGET_OUTSIDE_ROOT"
        if path.name.startswith(".env") or path.name.endswith(".pem") or "secret" in path.name.lower():
            return False, "SECRET_FILE_FORBIDDEN"
        if old == new:
            return False, "NO_CHANGE"
        if len(new.encode('utf-8')) > 120000:
            return False, "PATCH_TOO_LARGE"
        if path.name in PROTECTED_FILES:
            return False, "PROTECTED_SAFETY_FILE_FORBIDDEN"
        # A repair may never modify the safety authority, execution gates, or risk enforcement.
        # Presence-based checks are insufficient: compare semantic safety invariants and deny any
        # change to files containing protected policy terms.
        old_lower, new_lower = old.lower(), new.lower()
        for term in LIVE_BLOCK_TERMS:
            if term.lower() in old_lower and old != new:
                return False, f"PROTECTED_POLICY_MODIFICATION:{term}"
        if re.search(r"(?i)\bLIVE_TRADING\s*=\s*true\b", new) or re.search(r"(?i)os\.environ\s*\[[^]]*LIVE_TRADING[^]]*\]\s*=\s*[\'\"]true[\'\"]", new):
            return False, "LIVE_TRADING_ENABLE_FORBIDDEN"
        if re.search(r"(?i)\blive_orders_enabled\s*=\s*true\b", new) or re.search(r"(?i)os\.environ\s*\[[^]]*LIVE_ORDERS_ENABLED[^]]*\]\s*=\s*[\'\"]true[\'\"]", new):
            return False, "LIVE_ORDER_OR_ENV_ENABLE_FORBIDDEN"
        if re.search(r"(?i)def\s+place_order\s*\(", new):
            return False, "ORDER_FUNCTION_ADDITION_FORBIDDEN"
        return True, "OK"


class CodeSelfRepairEngine:
    VERSION = "v18.0-verified-code-self-repair"

    def __init__(self, root: Path, audit=None, provider: Optional[AIRepairProvider] = None,
                 test_command: Optional[list[str]] = None):
        self.root = Path(root).resolve()
        self.state_dir = self.root / "runtime" / "self_repair"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.audit = audit or (lambda *_: None)

        if provider is not None:
            self.provider = provider
        elif os.getenv("REPAIR_LLM_ENABLED", "false").lower()=="true" and os.getenv("OPENAI_API_KEY", "").strip():
            self.provider = OpenAIRepairProvider()
        else:
            self.provider = DeterministicRepairProvider()
        self.test_command = test_command or [sys.executable, "-m", "pytest", "-q"]
        self.policy = RepairPolicy(
            self.root,
            paper_only=os.getenv("LIVE_TRADING", "false").lower() != "true",
            live_orders_enabled=os.getenv("LIVE_ORDERS_ENABLED", "false").lower() == "true",
            require_tests=True,
            # Runtime configuration can enable automatic diagnosis/verification, but
            # ordinary environment variables must never grant source-promotion authority.
            # Promotion requires an explicit in-process approval path.
            allow_auto_promote=False,
        )
        self.last_result: Optional[RepairResult] = None
        # Serialize repair transactions: snapshot -> verify -> promote/rollback must be atomic
        # per engine instance. This prevents concurrent requests from racing on the same target
        # and corrupting snapshot/last-result state.
        self._repair_lock = threading.RLock()

    @staticmethod
    def _sha(data: str) -> str:
        import hashlib
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _audit(self, event: str, detail: dict[str, Any]) -> None:
        try:
            self.audit(event, detail)
        except Exception:
            pass

    def diagnose(self, error_text: str, target_file: Optional[Path] = None) -> Diagnosis:
        incident = str(uuid.uuid4())
        text = error_text or ""
        evidence: list[str] = []
        category, severity, root, confidence, repairable = "UNKNOWN", "MEDIUM", "Unknown failure", 0.25, False
        if "SyntaxError" in text:
            category, severity, root, confidence, repairable = "SYNTAX_ERROR", "HIGH", "Python syntax error", 0.98, False
            evidence.append("Python reported SyntaxError; automatic patch generation is not enabled for arbitrary syntax edits")
        elif re.search(r"ModuleNotFoundError|ImportError", text):
            category, severity, root, confidence, repairable = "MISSING_IMPORT", "HIGH", "Import/dependency resolution failure", 0.92, False
            evidence.append("Import failure detected")
        elif "AssertionError" in text or "FAILED" in text:
            category, severity, root, confidence, repairable = "TEST_REGRESSION", "HIGH", "Regression test failure", 0.95, False
            evidence.append("Automated test failure detected; patch requires an explicit repair proposal")
        elif "Traceback" in text or "Exception" in text:
            category, severity, root, confidence, repairable = "RUNTIME_EXCEPTION", "HIGH", "Unhandled runtime exception", 0.70, False
            evidence.append("Runtime traceback detected")
        else:
            evidence.append("No high-confidence known failure signature")
        if target_file:
            evidence.append(f"target={target_file}")
        d = Diagnosis(incident, category, severity, root, confidence, evidence,
                      [str(target_file)] if target_file else [], repairable,
                      "Generate a candidate patch, then run the mandatory verification gate")
        self._audit("SELF_REPAIR_DIAGNOSIS", asdict(d))
        return d

    def _static_security(self, workspace: Path, candidate_rel: str | None = None) -> tuple[bool, str]:
        violations = []
        # Candidate code is treated as hostile. Use a deny-by-capability AST gate
        # in addition to the OS sandbox. In particular, block Python reflection and
        # builtin recovery paths that can evade a simple import/call-name scan.
        dangerous_modules = {
            'os','subprocess','socket','ctypes','multiprocessing','signal','resource','pty',
            'shutil','pathlib','importlib','runpy','code','pickle','marshal','ftplib','telnetlib',
            'requests','httpx','aiohttp','urllib.request','http.client','sys','builtins','inspect',
            'gc','types','dis','pkgutil','site','pydoc'
        }
        dangerous_calls = {
            'eval','exec','compile','__import__','open','system','popen','Popen','run','call',
            'check_call','check_output','create_connection','remove','unlink','rmtree','rename',
            'replace','chmod','chown','makedirs','mkdir','write_text','write_bytes','putenv','unsetenv',
            'getattr','setattr','delattr','globals','locals','vars','breakpoint','help','input'
        }
        dangerous_attrs = {
            '__builtins__','__import__','__globals__','__subclasses__','__class__','__base__',
            '__bases__','__mro__','__getattribute__','__getattr__','__setattr__','__delattr__',
            '__code__','__loader__','__spec__','__reduce__','__reduce_ex__'
        }
        files=[workspace/candidate_rel] if candidate_rel else list(workspace.rglob('*.py'))
        for fp in files:
            if any(part in {'.venv','venv','__pycache__','.git'} for part in fp.parts):
                continue
            try:
                text=fp.read_text(encoding='utf-8'); tree=ast.parse(text, filename=str(fp))
            except Exception as e:
                violations.append(f'{fp}:AST_PARSE:{e}'); continue
            if re.search(r'(?i)\bLIVE_TRADING\s*=\s*true\b', text): violations.append(f'{fp}:LIVE_TRADING=true')
            if re.search(r'(?i)\blive_orders_enabled\s*=\s*true\b', text): violations.append(f'{fp}:LIVE_ORDERS_ENABLED=true')
            if re.search(r'(?i)def\s+(place_order|submit_order|execute_order)\s*\(', text): violations.append(f'{fp}:ORDER_FUNCTION')
            for node in ast.walk(tree):
                if isinstance(node,(ast.Import,ast.ImportFrom)):
                    for a in node.names:
                        if a.name in dangerous_modules or any(a.name.startswith(x+'.') for x in dangerous_modules):
                            violations.append(f'{fp}:{getattr(node,"lineno",0)}:DANGEROUS_IMPORT:{a.name}')
                if isinstance(node,ast.Call):
                    fn=node.func; name=fn.id if isinstance(fn,ast.Name) else fn.attr if isinstance(fn,ast.Attribute) else ''
                    if name in dangerous_calls: violations.append(f'{fp}:{getattr(node,"lineno",0)}:DANGEROUS_CALL:{name}')
                    if isinstance(fn,ast.Attribute) and isinstance(fn.value,ast.Name) and fn.value.id in dangerous_modules:
                        violations.append(f'{fp}:{getattr(node,"lineno",0)}:DANGEROUS_CAPABILITY:{fn.value.id}.{name}')
                if isinstance(node, ast.Attribute) and node.attr in dangerous_attrs:
                    violations.append(f'{fp}:{getattr(node,"lineno",0)}:DANGEROUS_REFLECTION:{node.attr}')
                if isinstance(node, ast.Name) and node.id in dangerous_attrs:
                    violations.append(f'{fp}:{getattr(node,"lineno",0)}:DANGEROUS_NAME:{node.id}')
        return (not violations, 'security invariants passed' if not violations else '\n'.join(sorted(set(violations))))

    def _sandbox_backend_available(self) -> bool:
        """Return whether the configured OS isolation boundary is actually usable.

        This is a capability probe, not a security bypass. If Linux namespaces or
        the container runtime cannot be initialized, verification remains fail-closed.
        Tests may use this probe to distinguish an unavailable host capability from
        a candidate verification failure; production code must never fall back to
        unsandboxed execution.
        """
        system = platform.system().lower()
        if system == 'linux':
            unshare = shutil.which('unshare')
            bash = shutil.which('bash')
            if not unshare or not bash:
                return False
            try:
                probe = subprocess.run(
                    [unshare, '--user', '--map-root-user', '--mount', '--pid', '--fork', '--net', bash, '-lc', 'true'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, check=False,
                )
                return probe.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        return bool(shutil.which('docker') or shutil.which('podman'))

    def _sandbox_run(self, command: list[str], workspace: Path, timeout: int):
        """Run verification in a fail-closed isolated environment.

        Linux uses user/mount/PID/network namespaces with a private /tmp and a
        writable candidate workspace only. Non-Linux hosts require Docker/Podman;
        there is deliberately no unsandboxed fallback.
        """
        system = platform.system().lower()
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        if system == 'linux':
            unshare = shutil.which('unshare')
            bash = shutil.which('bash')
            if not unshare or not bash:
                return subprocess.CompletedProcess(command, 126, 'OS_SANDBOX_UNAVAILABLE:linux-tools', '')
            cmd = ' '.join(shlex.quote(str(x)) for x in command)
            script = f"""set -eu
mount --make-rprivate /
mount -t tmpfs -o size=128m tmpfs /mnt
mkdir -p /mnt/workspace
mount --bind {shlex.quote(str(workspace))} /mnt/workspace
mount -o remount,bind,rw,nosuid,nodev /mnt/workspace
mount -t tmpfs -o size=128m,nosuid,nodev,noexec tmpfs /tmp
cd /mnt/workspace
exec {cmd}
"""
            argv = [unshare, '--user', '--map-root-user', '--mount', '--pid', '--fork', '--net', bash, '-lc', script]
        else:
            runtime = shutil.which('docker') or shutil.which('podman')
            if not runtime:
                return subprocess.CompletedProcess(command, 126, f'OS_SANDBOX_UNAVAILABLE:{system}:docker-or-podman', '')
            image = os.environ.get('V27_SANDBOX_IMAGE', 'python:3.12-slim')
            inner = [str(x) for x in command]
            if inner and (inner[0] in {'python', 'python3'} or inner[0].endswith('/python') or inner[0].lower().endswith('\\python.exe')):
                inner[0] = 'python'
            argv = [runtime, 'run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=512m', '--cpus=2',
                    '-v', f'{workspace}:/workspace:rw', '-w', '/workspace', image, *inner]

        proc = None
        try:
            proc = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                out, _ = proc.communicate(timeout=timeout)
                return subprocess.CompletedProcess(argv, proc.returncode, out)
            except subprocess.TimeoutExpired as e:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    out, _ = proc.communicate(timeout=3)
                except Exception:
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except Exception: pass
                    try: out, _ = proc.communicate(timeout=2)
                    except Exception: out = ''
                return subprocess.CompletedProcess(argv, 124, f'SANDBOX_TIMEOUT:{e}', out or '')
        except Exception as e:
            return subprocess.CompletedProcess(argv, 126, f'SANDBOX_EXEC_ERROR:{type(e).__name__}:{e}', '')
        finally:
            if proc is not None and proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
                try: proc.communicate(timeout=2)
                except Exception: pass

    def _integrity_manifest(self, workspace: Path, candidate_rel: str | None = None) -> dict[str, str]:
        """Hash meaningful workspace files while ignoring generated test/runtime caches."""
        import hashlib
        ignored = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
        manifest: dict[str, str] = {}
        root = Path(workspace).resolve()
        for fp in root.rglob('*'):
            if not fp.is_file() or any(part in ignored for part in fp.parts):
                continue
            try:
                rel = fp.relative_to(root).as_posix()
                h = hashlib.sha256(fp.read_bytes()).hexdigest()
                manifest[rel] = h
            except OSError:
                continue
        return manifest

    def _integrity_changed(self, before: dict[str, str], after: dict[str, str]) -> list[str]:
        changes = []
        for path in sorted(set(before) | set(after)):
            if path not in before:
                changes.append(f'ADDED:{path}')
            elif path not in after:
                changes.append(f'REMOVED:{path}')
            elif before[path] != after[path]:
                changes.append(f'MODIFIED:{path}')
        return changes

    def _verify(self, workspace: Path, candidate_rel: str | None = None) -> Verification:
        # Candidate identity is request-local; never fall back to mutable shared state.
        candidate_rel = candidate_rel or ""
        start=time.time()
        integrity_before = self._integrity_manifest(workspace, candidate_rel)
        compile_proc=self._sandbox_run([sys.executable,'-m','compileall','-q','.'],workspace,120)
        compile_ok=compile_proc.returncode==0
        test_ok=False; test_output=''
        if compile_ok and self.policy.require_tests:
            command=list(self.test_command)
            # The outer V18 self-repair tests are meta-tests that invoke this
            # verifier themselves. Running them inside candidate verification
            # would recursively spawn pytest forever. Keep the exclusion explicit
            # and narrowly scoped to that meta-test module; all production tests
            # remain mandatory. The outer suite executes V18 normally.
            if 'pytest' in command:
                command += ['--ignore=test_v18_self_repair.py']
            p=self._sandbox_run(command,workspace,300); test_ok=p.returncode==0; test_output=(p.stdout or '')[-12000:]
        elif compile_ok: test_ok=True
        integrity_after = self._integrity_manifest(workspace, candidate_rel)
        integrity_changes = self._integrity_changed(integrity_before, integrity_after)
        integrity_ok = not integrity_changes
        if not integrity_ok:
            test_ok = False

        security_ok,security_output=self._static_security(workspace, candidate_rel)
        if not integrity_ok:
            security_output += "\nVERIFICATION_WORKSPACE_INTEGRITY_FAIL:\n" + "\n".join(integrity_changes[:200])
        try:
            from security_verifier import verify_tree
            independent=verify_tree(workspace, candidate_files=[candidate_rel] if candidate_rel else None)
            security_ok=security_ok and independent['ok']
            security_output += "\nINDEPENDENT_VERIFIER:\n" + ("PASS" if independent['ok'] else "FAIL\n" + "\n".join(independent['violations']))
        except Exception as e:
            security_ok=False; security_output += f"\nINDEPENDENT_VERIFIER_ERROR:{e}"
        out=("COMPILE:\n"+(compile_proc.stdout or "")[-4000:]+"\nTESTS:\n"+test_output[-12000:]+"\nSECURITY:\n"+security_output)
        return Verification(compile_ok and test_ok and security_ok and integrity_ok,compile_ok,test_ok,security_ok,out,round(time.time()-start,3))
    def _snapshot(self, path: Path) -> Path:
        snap = self.state_dir / "snapshots" / f"{int(time.time())}_{uuid.uuid4().hex}"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.mkdir()
        shutil.copy2(path, snap / path.name)
        return snap / path.name

    def _restore(self, path: Path, snapshot: Path) -> None:
        shutil.copy2(snapshot, path)

    def _verify_isolated_copy(self, source_root: Path) -> Verification:
        """Verify an immutable snapshot/copy, never the live application tree.

        The sandbox intentionally mounts its workspace read-write because pytest may
        create caches and test fixtures. Therefore the workspace passed here must be
        disposable and must never be self.root. This prevents a candidate from
        modifying protected files during post-promotion verification.
        """
        temp = Path(tempfile.mkdtemp(prefix="self_repair_postverify_"))
        try:
            copy_root = temp / source_root.name
            shutil.copytree(
                source_root, copy_root, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", ".pytest_cache", "self_repair",
                    "self_repair_*", ".env", ".env.*", "*.pem", "*.key",
                    "*secret*", "*credential*"
                ),
            )
            return self._verify(copy_root)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def propose_and_verify(self, diagnosis: Diagnosis, target_file: Path,
                           new_source: Optional[str] = None, promote: bool = False) -> RepairResult:
        with self._repair_lock:
            return self._propose_and_verify_locked(diagnosis, target_file, new_source, promote)

    def _propose_and_verify_locked(self, diagnosis: Diagnosis, target_file: Path,
                                   new_source: Optional[str] = None, promote: bool = False) -> RepairResult:
        target_file = Path(target_file).resolve()
        ok, why = self.policy.validate()
        if not ok:
            result = RepairResult(False, "POLICY_BLOCKED", diagnosis, None, None, False, False, why)
            self.last_result = result
            return result
        if not target_file.exists() or not target_file.is_file():
            result = RepairResult(False, "TARGET_NOT_FOUND", diagnosis, None, None, False, False, str(target_file))
            self.last_result = result
            return result
        old = target_file.read_text(encoding="utf-8")
        candidate = new_source
        if candidate is None:
            try:
                candidate = self.provider.propose(diagnosis, old, str(target_file))
            except Exception as e:
                result = RepairResult(False, "PROVIDER_ERROR", diagnosis, None, None, False, False,
                                      f"Repair provider failed closed: {type(e).__name__}")
                self.last_result = result
                self._audit("SELF_REPAIR_PROVIDER_ERROR", {"error": type(e).__name__})
                return result
        if candidate is None:
            result = RepairResult(False, "NO_PATCH_PROPOSAL", diagnosis, None, None, False, False,
                                  "No high-confidence patch was generated")
            self.last_result = result
            return result
        allowed, reason = self.policy.patch_allowed(old, candidate, target_file)
        if not allowed:
            result = RepairResult(False, "PATCH_BLOCKED", diagnosis, None, None, False, False, reason)
            self.last_result = result
            self._audit("SELF_REPAIR_PATCH_BLOCKED", {"reason": reason, "file": str(target_file)})
            return result
        # Parse candidate before writing it anywhere.
        try:
            ast.parse(candidate, filename=str(target_file))
        except SyntaxError as e:
            result = RepairResult(False, "PATCH_SYNTAX_INVALID", diagnosis, None, None, False, False, str(e))
            self.last_result = result
            return result
        patch_id = str(uuid.uuid4())
        diff = "".join(difflib.unified_diff(old.splitlines(True), candidate.splitlines(True),
                                              fromfile=str(target_file), tofile=str(target_file)))
        proposal = PatchProposal(diagnosis.incident_id, patch_id, "PROVIDER", str(target_file),
                                 "Candidate source repair", self._sha(old), self._sha(candidate),
                                 diff, min(diagnosis.confidence, 0.99), "SAFE_CANDIDATE")
        self._audit("SELF_REPAIR_PATCH_PROPOSED", asdict(proposal))

        # TOCTOU guard: the source must still be exactly the version that was proposed against.
        # Otherwise a concurrent/manual edit could be silently overwritten or restored incorrectly.
        try:
            current = target_file.read_text(encoding="utf-8")
        except OSError as e:
            result = RepairResult(False, "TARGET_READ_FAILED", diagnosis, proposal, None, False, False, str(e))
            self.last_result = result
            return result
        if self._sha(current) != proposal.old_sha256:
            result = RepairResult(False, "TARGET_CHANGED_DURING_REPAIR", diagnosis, proposal, None, False, False,
                                  "Target changed after diagnosis/proposal; repair aborted fail-closed")
            self.last_result = result
            self._audit("SELF_REPAIR_TARGET_CHANGED", {"file": str(target_file), "patch_id": patch_id})
            return result
        snapshot = self._snapshot(target_file)
        workspace = Path(tempfile.mkdtemp(prefix="self_repair_"))
        try:
            shutil.copytree(self.root, workspace / self.root.name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "self_repair", "self_repair_*", ".env", ".env.*", "*.pem", "*.key", "*secret*", "*credential*"))
            ws_root = workspace / self.root.name
            ws_target = ws_root / target_file.relative_to(self.root)
            ws_target.write_text(candidate, encoding="utf-8")
            candidate_rel = str(target_file.relative_to(self.root))
            verification = self._verify(ws_root, candidate_rel)
            self._audit("SELF_REPAIR_VERIFICATION", {"patch_id": patch_id, "verification": asdict(verification)})
            if not verification.passed:
                result = RepairResult(False, "VERIFICATION_FAILED", diagnosis, proposal, verification,
                                      False, False, "Candidate rejected; original source remains unchanged")
                self.last_result = result
                return result
            if not promote or not self.policy.allow_auto_promote:
                result = RepairResult(True, "VERIFIED_CANDIDATE", diagnosis, proposal, verification,
                                      False, False, "Patch verified in isolated workspace; promotion not enabled")
                self.last_result = result
                return result
            # Promote only after all gates pass and only when explicitly enabled.
            target_file.write_text(candidate, encoding="utf-8")
            post = self._verify_isolated_copy(self.root)
            if post.passed:
                result = RepairResult(True, "PROMOTED", diagnosis, proposal, post, False, True,
                                      "Patch promoted and post-deploy verification passed")
                self.last_result = result
                self._audit("SELF_REPAIR_PROMOTED", asdict(proposal))
                return result
            self._restore(target_file, snapshot)
            rollback_verify = self._verify_isolated_copy(self.root)
            result = RepairResult(False, "ROLLED_BACK", diagnosis, proposal, rollback_verify,
                                  True, False, "Post-promotion verification failed; restored last-known-good source")
            self.last_result = result
            self._audit("SELF_REPAIR_ROLLBACK", {"patch_id": patch_id, "verification": asdict(rollback_verify)})
            return result
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def status(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "enabled": True,
            "paper_only": self.policy.paper_only,
            "live_orders_enabled": self.policy.live_orders_enabled,
            "auto_promote": self.policy.allow_auto_promote,
            "paper_auto_self_repair": os.getenv("PAPER_AUTO_SELF_REPAIR", "false").lower() == "true",
            "trading_mode": os.getenv("TRADING_MODE", "paper").lower(),
            "mandatory_tests": self.policy.require_tests,
            "last_result": asdict(self.last_result) if self.last_result else None,
        }
