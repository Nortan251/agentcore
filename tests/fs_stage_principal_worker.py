"""Privileged test setup only; all AgentCore / attack work uses ordinary UIDs."""

import ctypes  # noqa: F401 - mutation imports this lazily after the UID drop
import fcntl  # noqa: F401 - durable stores import this lazily after the UID drop
import json
import os
from pathlib import Path
import platform  # noqa: F401 - mutation imports this lazily after the UID drop
import pwd
import socket
import struct
import sys
import tempfile
import traceback


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RUNTIME_ROOT / "tests"), str(RUNTIME_ROOT / "src")]
# Load code before losing runtime search access; construct host state only below.
from test_fs_write import Harness
from agentcore.fs import mutation


def drop(uid):
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    assert os.getuid() == os.geteuid() == uid != 0
    assert os.getgid() == os.getegid() == uid and not os.getgroups()
    status = Path("/proc/self/status").read_text()
    assert "CapEff:\t0000000000000000" in status


def send(channel, value):
    channel.write(json.dumps(value).encode() + b"\n")
    channel.flush()


def receive(channel):
    line = channel.readline()
    if not line:
        raise RuntimeError("second-principal rendezvous closed")
    return json.loads(line)


def attack(sock, uid):
    drop(uid)
    with sock.makefile("rwb") as channel:
        while True:
            request = receive(channel)
            if request == "stop":
                return
            if request == "identity":
                send(channel, {"uid": os.geteuid(), "groups": os.getgroups()})
                continue
            path = Path(request)
            denied = False
            try:
                os.rename(path, path.with_name("displaced-stage"))
                path.write_bytes(b"substituted")
                # Let the victim hard-link this attacker-owned object even
                # with Linux protected_hardlinks=1. Do not accidentally make
                # that unrelated kernel policy mask the original exploit.
                os.chmod(path, 0o666)
            except PermissionError:
                denied = True
            send(channel, {"denied": denied, "uid": os.geteuid(), "groups": os.getgroups()})


def agent(sock, root, uid, attacker_uid, scenario):
    drop(uid)  # Must precede all guard / journal construction and execution.
    h = Harness(root / "host")
    os.chmod(h.base, 0o755)
    os.chmod(h.root, 0o755)
    target_parent = h.root / "sub"
    os.chmod(target_parent, 0o777 if scenario == "shared" else 0o755)
    if scenario == "acl-masked":
        # Genuine Linux access ACL: the attacker entry requests rwx, but the
        # group-class mask restricts it to r-x. No setfacl utility is required.
        entries = [(1, 7, 0xFFFFFFFF), (2, 7, attacker_uid),
                   (4, 5, 0xFFFFFFFF), (16, 5, 0xFFFFFFFF), (32, 5, 0xFFFFFFFF)]
        acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
        os.setxattr(target_parent, "system.posix_acl_access", acl)
        assert target_parent.stat().st_mode & 0o777 == 0o755
    with sock.makefile("rwb") as channel:
        facts = {"agent_uid": os.geteuid(), "agent_groups": os.getgroups(),
                 "runtime_accessible": os.access(RUNTIME_ROOT, os.X_OK),
                 "attacker_uid": attacker_uid, "attacker_groups": [],
                 "namespace_probe_substituted": False, "attack_reached": False, "attack_denied": False}
        send(channel, "identity")
        identity = receive(channel)
        facts["attacker_uid"], facts["attacker_groups"] = identity["uid"], identity["groups"]
        if scenario == "shared":
            # Prove the different UID actually has entry-mutation authority
            # over an Agent-owned object without reading that object's bytes.
            probe = target_parent / "probe"
            probe.write_bytes(b"owned")
            os.chmod(probe, 0o600)
            send(channel, str(probe))
            response = receive(channel)
            facts["namespace_probe_substituted"] = not response["denied"] and probe.read_bytes() == b"substituted"
            facts["attacker_uid"], facts["attacker_groups"] = response["uid"], response["groups"]
            probe.unlink()
            (target_parent / "displaced-stage").unlink()
        if scenario == "foreign":
            # Root setup placed the foreign-owned directory before the UID drop.
            target_parent = h.root / "foreign"
            assert target_parent.stat().st_uid == attacker_uid != os.geteuid()
        facts["parent_uid"] = target_parent.stat().st_uid
        facts["parent_mode"] = target_parent.stat().st_mode & 0o7777
        original_link = mutation.os.link
        def publication(stage, name, **kwargs):
            facts["attack_reached"] = True
            # This runs after the production owned/named stage identity check.
            send(channel, str(target_parent / stage))
            response = receive(channel)
            facts["attack_denied"] = response["denied"]
            facts["attacker_uid"], facts["attacker_groups"] = response["uid"], response["groups"]
            return original_link(stage, name, **kwargs)
        mutation.os.link = publication
        try:
            result = h.invoke(path="foreign/new.txt" if scenario == "foreign" else "sub/new.txt", content="intended")
            record = h.actions.load(result.action_id)
            target = target_parent / "new.txt"
            facts.update(ok=result.ok, bytes=target.read_text() if target.exists() else None,
                         entries=sorted(p.name for p in target_parent.iterdir()),
                         attempt=record.attempts[-1].status.value,
                         verdict=record.terminal_outcome.verdict.value if record.terminal_outcome else None)
            send(channel, "stop")
            return facts
        finally:
            h.close()


def main():
    assert sys.platform == "linux" and os.geteuid() == 0
    unused = []
    for uid in range(60000, 61000):
        try:
            pwd.getpwuid(uid)
        except KeyError:
            unused.append(uid)
        if len(unused) == 2:
            break
    assert len(unused) == 2
    agent_uid, attacker_uid = unused
    with tempfile.TemporaryDirectory(prefix="agentcore-two-principals-") as directory:
        root = Path(directory)
        os.chown(root, agent_uid, agent_uid)
        os.chmod(root, 0o755)
        # Linux requires write access to a moved directory when changing its parent.
        for parent in (root / "host", root / "host/workspace"):
            parent.mkdir(mode=0o755)
            os.chown(parent, agent_uid, agent_uid)
        foreign = root / "host/workspace/foreign"
        foreign.mkdir(mode=0o755)
        os.chown(foreign, attacker_uid, attacker_uid)
        os.chmod(foreign, 0o755)
        left, right = socket.socketpair()
        left.settimeout(20)
        right.settimeout(20)
        read_fd, write_fd = os.pipe()
        attacker = os.fork()
        if attacker == 0:
            left.close()
            os.close(read_fd)
            os.close(write_fd)
            try:
                attack(right, attacker_uid)
                os._exit(0)
            except BaseException:
                traceback.print_exc()
                os._exit(1)
        child = os.fork()
        if child == 0:
            right.close()
            os.close(read_fd)
            try:
                facts = agent(left, root, agent_uid, attacker_uid, sys.argv[1])
                os.write(write_fd, json.dumps(facts).encode())
                os._exit(0)
            except BaseException:
                traceback.print_exc()
                os._exit(1)
        left.close()
        right.close()
        os.close(write_fd)
        with os.fdopen(read_fd) as stream:
            facts = stream.read()
        statuses = [os.waitpid(pid, 0)[1] for pid in (child, attacker)]
        assert statuses == [0, 0], statuses
        print(facts)


if __name__ == "__main__":
    main()
