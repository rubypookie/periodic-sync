import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def git(*args):
    result = subprocess.run(
        ["git", *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
    )
    if result.returncode:
        diagnostic = "remote or Git error"
        for symptom in ("error in libcrypto", "Permission denied", "Host key verification failed",
                        "Could not resolve hostname", "Connection timed out", "Repository not found",
                        "remote unpack failed", "fatal: bad object"):
            if symptom in result.stderr:
                diagnostic = symptom
                break
        raise RuntimeError(f"Git operation failed ({diagnostic}). Existing remote backups were preserved.")
    return result.stdout.strip()


def refs(repo):
    output = git("-C", repo, "for-each-ref", "--format=%(refname) %(objectname)",
                 "refs/heads", "refs/tags")
    return dict(line.split(" ", 1) for line in output.splitlines() if line)


def synchronize(source_url, backup_url, workspace, dependency_url=None):
    source = workspace / "source.git"
    backup = workspace / "backup.git"

    git("ls-remote", backup_url)
    try:
        git("clone", "--mirror", "--quiet", source_url, source)
    except RuntimeError:
        print("Source unavailable. Existing private backup and retained history were left intact.")
        return
    git("-C", source, "fsck", "--full")
    source_refs = refs(source)
    if not source_refs:
        raise RuntimeError("Source has no branches or tags. Remote backup was preserved.")
    if any(ref.startswith("refs/tags/retained/") for ref in source_refs):
        raise RuntimeError("Source uses the reserved retention namespace.")
    git("clone", "--mirror", "--quiet", backup_url, backup)
    previous_refs = refs(backup)
    current_refs = {k: v for k, v in previous_refs.items()
                    if not k.startswith(("refs/tags/retained/", "refs/tags/dependencies/"))}
    changed = any(current_refs.get(k) != v for k, v in source_refs.items())
    if dependency_url:
        tree = git("-C", source, "ls-tree", "-r", "HEAD")
        pinned = [line.split()[2] for line in tree.splitlines() if line.startswith("160000 ")]
        if len(pinned) > 1:
            raise RuntimeError("Additional submodules need configuration. Existing backup preserved.")
        missing = [sha for sha in pinned if f"refs/tags/dependencies/{sha}" not in previous_refs]
        if missing:
            dependency = workspace / "dependency.git"
            git("clone", "--mirror", "--quiet", dependency_url, dependency)
            for sha in missing:
                git("-C", dependency, "cat-file", "-e", f"{sha}^{{commit}}")
            git("-C", dependency, "fsck", "--full")
            git("-C", backup, "fetch", "--quiet", "--no-tags", dependency,
                "+refs/heads/*:refs/dependency-import/heads/*",
                "+refs/tags/*:refs/dependency-import/tags/*")
            for sha in set(refs(dependency).values()) | set(missing):
                git("-C", backup, "update-ref", f"refs/tags/dependencies/{sha}", sha)
            changed = True
    if not changed:
        print("Already current. All retained history is preserved.")
        return

    git("-C", backup, "fetch", "--quiet", "--no-tags", source,
        "+refs/heads/*:refs/incoming/heads/*",
        "+refs/tags/*:refs/incoming/tags/*")

    for ref, object_id in previous_refs.items():
        if not ref.startswith(("refs/tags/retained/", "refs/tags/dependencies/")):
            git("-C", backup, "update-ref", f"refs/tags/retained/{object_id}", object_id)
    for ref, object_id in source_refs.items():
        git("-C", backup, "update-ref", f"refs/tags/retained/{object_id}", object_id)
        git("-C", backup, "update-ref", ref, object_id)
    git("-C", backup, "fsck", "--full")

    git("-C", backup, "push", "--quiet", "--atomic", backup_url,
        "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    print(f"Synchronization completed. {len(source_refs)} source refs checked; full history retained.")


def main():
    source_url = os.environ["SOURCE_URL"].strip()
    backup_url = os.environ["BACKUP_URL"].strip()
    private_key = os.environ["SYNC_KEY"]
    with tempfile.TemporaryDirectory(prefix="repository-sync-") as temporary:
        workspace = Path(temporary)
        key = workspace / "key"
        key.write_text(private_key.replace("\r", "").rstrip() + "\n")
        key.chmod(0o600)
        request = urllib.request.Request("https://api.github.com/meta",
                                         headers={"User-Agent": "repository-sync"})
        with urllib.request.urlopen(request, timeout=30) as response:
            host_keys = json.load(response)["ssh_keys"]
        known_hosts = workspace / "known_hosts"
        known_hosts.write_text("".join(f"github.com {key}\n" for key in host_keys))
        os.environ["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={known_hosts}"
        )
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
        synchronize(source_url, backup_url, workspace, os.environ.get("DEPENDENCY_URL", "").strip())


if __name__ == "__main__":
    main()
