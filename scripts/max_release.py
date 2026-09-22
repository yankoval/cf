#!/usr/bin/env python3
"""Build and deploy MAX releases without writing cloud credentials to disk."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "max-messenger-notifier"
FUNCTION_ID = "d4ep5laqq7maeesp0lcd"
STABLE = "production-stable"
API = "https://serverless-functions.api.cloud.yandex.net/functions/v1"
FILES = ("index.py", "requirements.txt", "DejaVuSans.ttf")
CONFIG_KEYS = (
    "runtime", "entrypoint", "resources", "executionTimeout", "serviceAccountId",
    "environment", "namedServiceAccounts", "concurrency", "connectivity", "secrets",
    "logOptions", "storageMounts", "asyncInvocationConfig", "tmpfsSize", "mounts",
    "metadataOptions",
)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()


def package_bytes(commit, sources):
    """Fixed ZIP metadata makes the same commit and source bytes reproducible."""
    hashes = {name: sha256(value) for name, value in sorted(sources.items())}
    info = {"repository": "yankoval/cf", "commit": commit,
            "component": "max-messenger-notifier", "files": hashes,
            "source_sha256": sha256(json_bytes(hashes))}
    payload = dict(sources, **{"build-info.json": json_bytes(info)})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in sorted(payload.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, value, compresslevel=9)
    return output.getvalue(), dict(info, package_sha256=sha256(output.getvalue()))


def build(output):
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Build requires a clean tracked Git tree")
    commit = git("rev-parse", "HEAD")
    if os.environ.get("GITHUB_SHA", commit) != commit:
        raise RuntimeError("Checkout does not match the workflow commit")
    sources = {name: (COMPONENT / name).read_bytes() for name in FILES}
    # Read only the allowlisted runtime files; no credentials or release tools.
    package, manifest = package_bytes(commit, sources)
    output.mkdir(parents=True, exist_ok=True)
    (output / "max-notifier.zip").write_bytes(package)
    (output / "manifest.json").write_bytes(json_bytes(manifest))
    (output / "SHA256SUMS").write_text(manifest["package_sha256"] + "  max-notifier.zip\n")
    print(json.dumps({"commit": commit, "package_sha256": manifest["package_sha256"]}))


def verify_package(output, commit):
    package = (output / "max-notifier.zip").read_bytes()
    manifest = json.loads((output / "manifest.json").read_bytes())
    if manifest["commit"] != commit or manifest["package_sha256"] != sha256(package):
        raise RuntimeError("Artifact commit or package checksum mismatch")
    if manifest["source_sha256"] != sha256(json_bytes(manifest["files"])):
        raise RuntimeError("Source manifest checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        names = archive.namelist()
        if sorted(names) != sorted((*FILES, "build-info.json")):
            raise RuntimeError("Unexpected or duplicate runtime files in artifact")
        for name in FILES:
            data = archive.read(name)
            if sha256(data) != manifest["files"][name] or data != (COMPONENT / name).read_bytes():
                raise RuntimeError("Artifact differs from the checked-out source: " + name)
        info = json.loads(archive.read("build-info.json"))
        if info != {key: value for key, value in manifest.items() if key != "package_sha256"}:
            raise RuntimeError("Embedded build manifest mismatch")
    return package, manifest


def request_json(url, *, token=None, value=None, form=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if value is not None:
        data = json_bytes(value)
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=150) as response:
            return json.load(response)
    except HTTPError as error:
        # API error bodies may contain parts of the submitted environment.
        raise RuntimeError(f"HTTP {error.code} from cloud/GitHub endpoint") from None


def github_token():
    if os.environ.get("GITHUB_REPOSITORY") != "yankoval/cf":
        raise RuntimeError("Deployment is restricted to yankoval/cf")
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise RuntimeError("Only explicit GitHub workflow dispatch can deploy")
    if os.environ.get("GITHUB_REF") != "refs/heads/" + os.environ["DEFAULT_BRANCH"]:
        raise RuntimeError("Deployment must run from the default branch")
    oidc_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    separator = "&" if "?" in oidc_url else "?"
    jwt = request_json(oidc_url + separator + urlencode({"audience": "https://github.com/yankoval"}),
                       token=os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"])["value"]
    token = request_json("https://auth.yandex.cloud/oauth/token", form={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": os.environ["YC_MAX_DEPLOY_SA_ID"], "subject_token": jwt,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
    })["access_token"]
    print("::add-mask::" + token)
    return token


class Cloud:
    def __init__(self, token):
        self.token = token

    def call(self, path, value=None):
        return request_json(API + path, token=self.token, value=value)

    def operation(self, operation):
        deadline = time.monotonic() + 900
        while not operation.get("done"):
            if time.monotonic() > deadline:
                raise RuntimeError("Cloud operation timed out: " + operation["id"])
            time.sleep(5)
            operation = self.call("/operations/" + operation["id"])
        if "error" in operation:
            raise RuntimeError("Cloud operation failed: " + operation["id"] +
                               " code=" + str(operation["error"].get("code")))
        return operation.get("response", {})

    def stable(self):
        return self.call("/versions:byTag?" + urlencode({"functionId": FUNCTION_ID, "tag": STABLE}))

    def set_tag(self, version_id, tag):
        self.operation(self.call("/versions/" + version_id + ":setTag", {"tag": tag}))

    def smoke(self, tag):
        # An empty event loads the real runtime/config but reads no reports and sends no messages.
        result = request_json("https://functions.yandexcloud.net/" + FUNCTION_ID + "?" +
                              urlencode({"tag": tag, "integration": "raw"}),
                              token=self.token, value={"messages": []})
        if result != {"statusCode": 200, "body": "OK"}:
            raise RuntimeError("Candidate empty-event smoke check failed")


def config(version):
    return {key: version[key] for key in CONFIG_KEYS if key in version}


def require_current(cloud, expected):
    version = cloud.stable()
    if version["id"] != expected or version["functionId"] != FUNCTION_ID:
        raise RuntimeError("production-stable changed; inspect before retrying")
    return version


def deploy(output, expected, rollback=None):
    if not re.fullmatch(r"[a-z0-9]{20}", expected):
        raise RuntimeError("Expected current version ID is required")
    commit = git("rev-parse", "HEAD")
    if commit != os.environ["GITHUB_SHA"] or git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Deployment checkout differs from the workflow commit")
    package, manifest = verify_package(output, commit)
    cloud = Cloud(github_token())
    previous = require_current(cloud, expected)
    run = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    run_url = f"https://github.com/yankoval/cf/actions/runs/{run}"
    candidate_tag = f"gh-{run}-{attempt}"
    record = {"repository": "yankoval/cf", "function_id": FUNCTION_ID,
              "workflow_commit": commit, "run_url": run_url,
              "previous_version_id": expected, "stable_tag": STABLE, "status": "started"}
    output.mkdir(parents=True, exist_ok=True)

    def save():
        (output / "deployment.json").write_bytes(json_bytes(record))

    save()
    if rollback:
        if not re.fullmatch(r"[a-z0-9]{20}", rollback):
            raise RuntimeError("Invalid rollback version ID")
        candidate = cloud.call("/versions/" + rollback)
        if candidate["functionId"] != FUNCTION_ID:
            raise RuntimeError("Rollback target belongs to another function")
        cloud.set_tag(candidate["id"], candidate_tag)
        record.update(action="rollback", target_description=candidate.get("description", ""))
    else:
        if previous["runtime"] != "python314" or previous["entrypoint"] != "index.handler":
            raise RuntimeError("Unexpected existing runtime or entrypoint")
        body = config(previous)
        body.update(functionId=FUNCTION_ID, content=base64.b64encode(package).decode(),
                    tag=[candidate_tag], description=(f"git={commit}; zip-sha256={manifest['package_sha256']}; "
                                                      f"source-sha256={manifest['source_sha256']}; run={run_url}"))
        operation = cloud.call("/versions", body)
        record.update(action="deploy", manifest=manifest, operation_id=operation["id"])
        save()
        candidate = cloud.operation(operation)
        candidate = cloud.call("/versions/" + candidate["id"])
        if config(candidate) != config(previous):
            raise RuntimeError("Candidate configuration differs from the previous production version")
    record.update(version_id=candidate["id"], candidate_tag=candidate_tag, status="candidate")
    save()
    cloud.smoke(candidate_tag)
    record.update(smoke="empty-event-passed", status="smoke-passed")
    save()
    require_current(cloud, expected)
    cloud.set_tag(expected, f"rollback-{run}-{attempt}")
    cloud.set_tag(candidate["id"], STABLE)
    require_current(cloud, candidate["id"])
    record["status"] = "production"
    save()
    print(json.dumps({key: record[key] for key in ("version_id", "previous_version_id", "status", "run_url")}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "deploy"))
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "max-notifier")
    parser.add_argument("--expected-current-version", default="")
    parser.add_argument("--rollback-version", default="")
    args = parser.parse_args()
    if args.command == "build":
        build(args.output)
    else:
        deploy(args.output, args.expected_current_version, args.rollback_version or None)


if __name__ == "__main__":
    main()
