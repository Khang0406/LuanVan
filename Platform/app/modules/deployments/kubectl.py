import json
import subprocess
import time
from datetime import datetime
from typing import Any

from app.modules.applications.service import add_activity, save_application
from app.modules.deployments.manifest import write_manifest


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_kubectl(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    command = ["kubectl", *args]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        output = (completed.stdout + completed.stderr).strip()
        return completed.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"kubectl timeout sau {timeout} giây: {' '.join(command)}"
    except FileNotFoundError:
        return False, "Không tìm thấy kubectl trên máy chạy platform."


def deploy_application(application: dict[str, Any]) -> tuple[bool, str]:
    """Deploy application to K3s cluster.

    For GitHub-sourced applications, the pipeline must have already built
    Docker images and updated service.image fields. If no image is set on
    the first service, the deployment is rejected.
    """
    # Verify at least one service has an image
    services = application.get("services", [])
    if not services:
        return False, "Application has no services defined"

    primary_image = services[0].get("image", "")
    if not primary_image:
        add_activity(
            application,
            "DEPLOY",
            "Chưa có Docker image — hãy trigger pipeline để build từ GitHub source trước.",
            "Planned",
        )
        application["status"] = "Pending Build"
        save_application(application)
        return False, "Chưa có Docker image cho application. Vui lòng trigger pipeline từ CI/CD để build trước."

    manifest_path = write_manifest(application)
    add_activity(application, "MANIFEST", f"Generated Kubernetes manifest: {manifest_path}", "Done")

    # K3s clusters installed by this platform can use a self-signed API server
    # certificate. `kubectl apply` tries to download the OpenAPI schema for
    # client-side validation before applying resources; with an untrusted/self-
    # signed certificate this fails before the manifest reaches the cluster.
    # Server-side validation/admission still happens on the API server, while
    # this flag prevents the local OpenAPI download from blocking deployment.
    #
    # Pre-flight cluster health check — verify API server is reachable
    # before attempting apply. This is critical after deleteworkload
    # operations which can temporarily destabilize single-node K3s.
    for preflight_attempt in range(1, 4):
        health_ok, _ = run_kubectl(["cluster-info"], timeout=15)
        if health_ok:
            break
        if preflight_attempt < 3:
            time.sleep(15)

    # Retry up to 5 times with backoff to handle intermittent connectivity
    # issues (TLS timeout, connection refused, API server temporarily
    # unavailable after delete operations).
    max_retries = 5
    last_output = ""
    for attempt in range(1, max_retries + 1):
        success, output = run_kubectl(["apply", "--validate=false", "-f", str(manifest_path)], timeout=90)
        if success:
            application["status"] = "Running"
            application["last_deployed_at"] = _now()
            application["url"] = discover_application_url(application)
            add_activity(application, "DEPLOY", "Deploy application lên K3s thành công.", "Done")
            save_application(application)
            return True, output

        last_output = output
        # Detect any network/server-side error that should be retried
        is_retryable = any(phrase in output.lower() for phrase in [
            "tls handshake timeout",
            "connection refused",
            "no such host",
            "error when retrieving current configuration",
            "i/o timeout",
            "context deadline exceeded",
            "unable to connect to the server",
            "server was unable to respond",
            "eof",
            "connection reset by peer",
            "tls: first record does not look like a tls handshake",
            "dial tcp",
            "connect:",
        ])
        if is_retryable and attempt < max_retries:
            wait = min(attempt * 10, 30)  # 10s, 20s, 30s, 30s
            time.sleep(wait)
            continue
        break

    application["status"] = "Deploy Failed"
    add_activity(application, "DEPLOY", f"Deploy application thất bại sau {max_retries} lần thử: {last_output[:300]}", "Failed")
    save_application(application)
    return False, last_output


def _run_kubectl_with_retry(args: list[str], timeout: int = 60, max_retries: int = 3) -> tuple[bool, str]:
    """Run a kubectl command with retry on transient TLS / connectivity errors."""
    _RETRYABLE = (
        "tls handshake timeout",
        "connection refused",
        "no such host",
        "i/o timeout",
        "context deadline exceeded",
        "unable to connect to the server",
        "eof",
        "connection reset by peer",
        "dial tcp",
        "connect:",
        "tls: first record does not look like a tls handshake",
    )
    last_ok, last_out = False, ""
    for attempt in range(1, max_retries + 1):
        ok, out = run_kubectl(args, timeout=timeout)
        if ok:
            return True, out
        last_ok, last_out = ok, out
        if any(phrase in out.lower() for phrase in _RETRYABLE) and attempt < max_retries:
            time.sleep(min(attempt * 10, 30))
            continue
        break
    return last_ok, last_out


def delete_application_workloads(application: dict[str, Any]) -> tuple[bool, str]:
    """Delete all K3s resources for an application (service → deploy → HPA → PVC → pod).

    NOTE: Does NOT delete the namespace itself — only cleans resources inside it.
    Deleting the namespace causes a race condition where the redeploy creates resources
    before the old namespace finishes terminating, resulting in 0 running pods at VERIFY.

    Each step retries up to 3 times on transient TLS / connectivity errors so that
    intermittent K3s API-server hiccups don't leave resources orphaned.
    """
    namespace = application["namespace"]
    logs: list[str] = []
    all_ok = True

    # Pre-flight: wait for API server to become reachable (up to ~45 s)
    for preflight_attempt in range(1, 4):
        health_ok, _ = run_kubectl(["cluster-info"], timeout=15)
        if health_ok:
            break
        if preflight_attempt < 3:
            time.sleep(15)

    # 1) Delete Service first (top-level resource that can block recreating)
    for svc in application.get("services", []):
        name = f"{application['id']}-{svc['name']}"
        ok, out = _run_kubectl_with_retry(["delete", "svc", name, "-n", namespace, "--ignore-not-found=true"], timeout=30)
        if not ok:
            all_ok = False
        logs.append(f"svc/{name}: {out[:100]}")
        # Also delete by label selector as fallback
        _run_kubectl_with_retry(["delete", "svc", "-n", namespace, "-l", f"app.kubernetes.io/name={name}", "--ignore-not-found=true"], timeout=20)

    # 2) Delete Deployment
    for svc in application.get("services", []):
        name = f"{application['id']}-{svc['name']}"
        ok, out = _run_kubectl_with_retry(["delete", "deploy", name, "-n", namespace, "--ignore-not-found=true"], timeout=30)
        if not ok:
            all_ok = False
        logs.append(f"deploy/{name}: {out[:100]}")
        _run_kubectl_with_retry(["delete", "deploy", "-n", namespace, "-l", f"app.kubernetes.io/name={name}", "--ignore-not-found=true"], timeout=20)

    # 3) Delete HPA if any
    for svc in application.get("services", []):
        if svc.get("autoscaling"):
            name = f"{application['id']}-{svc['name']}"
            _run_kubectl_with_retry(["delete", "hpa", name, "-n", namespace, "--ignore-not-found=true"], timeout=20)

    # 4) Delete PVC & Pod (old data cleanup without destroying namespace)
    # Patch PVC finalizer to null first to unblock
    _run_kubectl_with_retry(
        ["patch", "-n", namespace, "--all", "pvc", "-p", '{"metadata":{"finalizers":null}}', "--type=merge", "--ignore-not-found=true"],
        timeout=20,
    )
    ok_pvc, out_pvc = _run_kubectl_with_retry(
        ["delete", "pvc", "--all", "-n", namespace, "--ignore-not-found=true", "--force", "--grace-period=0"],
        timeout=30,
    )
    logs.append(f"pvc: {out_pvc[:100]}")
    ok_pod, out_pod = _run_kubectl_with_retry(
        ["delete", "pod", "--all", "-n", namespace, "--ignore-not-found=true", "--force", "--grace-period=0"],
        timeout=30,
    )
    logs.append(f"pod: {out_pod[:100]}")

    # 5) Wait for all pods to actually terminate before proceeding.
    #    Without this, new deployments create pods alongside old stuck ones,
    #    causing multiple Pending pods that prevent the service from working.
    for wait_attempt in range(12):  # up to 60s total
        ok_pods, pod_json = run_kubectl(
            ["get", "pods", "-n", namespace, "-l", f"app.kubernetes.io/part-of={application['id']}",
             "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=10,
        )
        if ok_pods and pod_json.strip():
            time.sleep(5)
        else:
            break  # no pods left, good
    # If pods still exist after 60s, hard-delete again with --all
    for retry_pass in range(2):
        ok_pods, pod_json = run_kubectl(
            ["get", "pods", "-n", namespace, "-l", f"app.kubernetes.io/part-of={application['id']}",
             "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=10,
        )
        if not ok_pods or not pod_json.strip():
            break
        _run_kubectl_with_retry(
            ["delete", "pod", "--all", "-n", namespace, "--ignore-not-found=true", "--force", "--grace-period=0"],
            timeout=30, max_retries=2,
        )
        time.sleep(5)

    # 6) Delete ConfigMaps that may have been created by the pipeline (e.g. init-sql)
    #    Keep namespace alive − redeploy will reuse it.
    _run_kubectl_with_retry(
        ["delete", "configmap", "-n", namespace, "-l", f"app.kubernetes.io/part-of={application['id']}", "--ignore-not-found=true"],
        timeout=30,
    )

    output = "\n".join(logs)
    if all_ok:
        application["status"] = "Deleted"
        application["url"] = ""
        add_activity(application, "DELETE", "Đã xoá workloads của application khỏi K3s (keeping namespace).", "Done")
    else:
        add_activity(application, "DELETE", f"Xoá workloads có lỗi: {output[:300]}", "Warning")
    save_application(application)
    return all_ok, output


def restart_application(application: dict[str, Any]) -> tuple[bool, str]:
    namespace = application["namespace"]
    outputs: list[str] = []
    all_success = True

    for service in application.get("services", []):
        deployment_name = f"{application['id']}-{service['name']}"
        success, output = run_kubectl(["rollout", "restart", f"deployment/{deployment_name}", "-n", namespace])
        all_success = all_success and success
        outputs.append(output)

    if all_success:
        add_activity(application, "RESTART", "Restart rollout cho tất cả deployment thành công.", "Done")
    else:
        add_activity(application, "RESTART", "Có deployment restart thất bại.", "Failed")
    save_application(application)
    return all_success, "\n".join(outputs)


def scale_application(application: dict[str, Any], replicas: int) -> tuple[bool, str]:
    namespace = application["namespace"]
    outputs: list[str] = []
    all_success = True

    for service in application.get("services", []):
        deployment_name = f"{application['id']}-{service['name']}"
        success, output = run_kubectl(["scale", f"deployment/{deployment_name}", f"--replicas={replicas}", "-n", namespace])
        all_success = all_success and success
        outputs.append(output)
        service["replicas"] = replicas

    if all_success:
        add_activity(application, "SCALE", f"Scale application lên {replicas} replicas/service thành công.", "Done")
    else:
        add_activity(application, "SCALE", f"Scale application lên {replicas} replicas/service thất bại.", "Failed")
    save_application(application)
    return all_success, "\n".join(outputs)


def get_application_logs(application: dict[str, Any], tail: int = 120) -> tuple[bool, str]:
    namespace = application["namespace"]
    outputs: list[str] = []
    all_success = True

    for service in application.get("services", []):
        deployment_name = f"{application['id']}-{service['name']}"
        success, output = run_kubectl(["logs", f"deployment/{deployment_name}", "-n", namespace, f"--tail={tail}"], timeout=45)
        all_success = all_success and success
        outputs.append(f"### {deployment_name}\n{output}")

    add_activity(application, "LOGS", "Người dùng truy vấn runtime logs của application.", "Done" if all_success else "Warning")
    save_application(application)
    return all_success, "\n\n".join(outputs)


def get_application_status(application: dict[str, Any]) -> dict[str, Any]:
    namespace = application["namespace"]
    result: dict[str, Any] = {
        "deployments": [],
        "pods": [],
        "services": [],
        "raw_error": "",
    }

    for resource_key, kubectl_resource in [
        ("deployments", "deployments"),
        ("pods", "pods"),
        ("services", "services"),
    ]:
        success, output = run_kubectl(
            [
                "get",
                kubectl_resource,
                "-n",
                namespace,
                "-l",
                f"app.kubernetes.io/part-of={application['id']}",
                "-o",
                "json",
            ],
            timeout=30,
        )
        if not success:
            result["raw_error"] += output + "\n"
            continue

        try:
            payload = json.loads(output)
            result[resource_key] = payload.get("items", [])
        except json.JSONDecodeError:
            result["raw_error"] += output + "\n"

    return result


def _get_node_ip() -> str:
    """Return the IP that developers can use to reach NodePort services.

    Priority:
    1. Kubeconfig server IP – this is the address developers already use for kubectl.
    2. `ExternalIP` of any node (e.g. host-only adapter).
    3. `InternalIP` of the first node.
    """
    # 1) Extract IP from kubeconfig server URL (the most developer-facing address)
    success, output = run_kubectl(["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"], timeout=10)
    if success and output.startswith("https://"):
        host_part = output[len("https://"):].split(":")[0]
        if host_part:
            return host_part

    # 2) Try ExternalIP / InternalIP from node status
    success, output = run_kubectl(["get", "nodes", "-o", "json"], timeout=15)
    if not success:
        return "localhost"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return "localhost"

    external_candidate = None
    internal_candidate = None
    for node in payload.get("items", []):
        for addr in node.get("status", {}).get("addresses", []):
            addr_type = addr.get("type", "")
            addr_val = addr.get("address", "")
            if addr_type == "ExternalIP":
                external_candidate = external_candidate or addr_val
            elif addr_type == "InternalIP":
                internal_candidate = internal_candidate or addr_val
    return external_candidate or internal_candidate or "localhost"


def discover_application_url(application: dict[str, Any]) -> str:
    namespace = application["namespace"]
    for service in application.get("services", []):
        service_name = f"{application['id']}-{service['name']}"
        success, output = run_kubectl(["get", "svc", service_name, "-n", namespace, "-o", "json"], timeout=20)
        if not success:
            continue

        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            continue

        service_type = payload.get("spec", {}).get("type")
        ports = payload.get("spec", {}).get("ports", [])
        if service_type == "NodePort" and ports:
            node_port = ports[0].get("nodePort")
            if node_port:
                node_ip = _get_node_ip()
                return f"http://{node_ip}:{node_port}"
        if service_type == "LoadBalancer":
            ingress = payload.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if ingress:
                host = ingress[0].get("ip") or ingress[0].get("hostname")
                if host:
                    return f"http://{host}:{ports[0].get('port', 80)}"

    return ""
