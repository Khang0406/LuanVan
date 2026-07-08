from pathlib import Path
from typing import Any

from app.config import BASE_DIR

GENERATED_DIR = BASE_DIR / "k8s" / "generated"


def _yaml_value(value: str) -> str:
    escaped = str(value).replace('"', '\\"')
    return f'"{escaped}"'


def build_manifest(application: dict[str, Any]) -> str:
    namespace = application["namespace"]
    documents: list[str] = [
        f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
  labels:
    managed-by: luanvan-platform
    platform/application: {application["id"]}
"""
    ]

    for service in application.get("services", []):
        app_name = f"{application['id']}-{service['name']}"
        env_items = service.get("env", [])
        env_yaml = ""
        if env_items:
            env_yaml = "\n          env:\n" + "\n".join(
                f"            - name: {item['name']}\n              value: {_yaml_value(item['value'])}" for item in env_items
            )

        resources_yaml = f"""
          resources:
            requests:
              cpu: {service.get("cpu_request", "100m")}
              memory: {service.get("memory_request", "128Mi")}
            limits:
              cpu: {service.get("cpu_limit", "500m")}
              memory: {service.get("memory_limit", "512Mi")}"""

        # Build volume mounts and volumes if hostPath is configured
        host_path = service.get("host_path", "")
        container_mount = service.get("container_mount", "")
        node_name = service.get("node_name", "")
        node_name_yaml = ""
        volume_mount_yaml = ""
        volumes_yaml = ""

        if host_path and container_mount:
            volume_mount_yaml = f"""
          volumeMounts:
            - name: app-code
              mountPath: {container_mount}"""
            volumes_yaml = f"""
      volumes:
        - name: app-code
          hostPath:
            path: {host_path}
            type: Directory"""

        if node_name:
            node_name_yaml = f"""
      nodeName: {node_name}"""

        documents.append(
            f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {app_name}
    app.kubernetes.io/part-of: {application["id"]}
    managed-by: luanvan-platform
spec:
  replicas: {int(service.get("replicas", 1))}
  selector:
    matchLabels:
      app.kubernetes.io/name: {app_name}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {app_name}
        app.kubernetes.io/part-of: {application["id"]}
    spec:{node_name_yaml}
      containers:
        - name: {service["name"]}
          image: {service["image"]}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: {int(service.get("container_port", 80))}{env_yaml}{resources_yaml}{volume_mount_yaml}{volumes_yaml}
"""
        )

        node_port = service.get("node_port", "")
        node_port_yaml = f"\n    nodePort: {node_port}" if service.get("service_type") == "NodePort" and node_port else ""

        documents.append(
            f"""apiVersion: v1
kind: Service
metadata:
  name: {app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {app_name}
    app.kubernetes.io/part-of: {application["id"]}
    managed-by: luanvan-platform
spec:
  type: {service.get("service_type", "NodePort")}
  selector:
    app.kubernetes.io/name: {app_name}
  ports:
    - name: http
      port: {int(service.get("container_port", 80))}
      targetPort: {int(service.get("container_port", 80))}{node_port_yaml}
"""
        )

        if service.get("autoscaling"):
            documents.append(
                f"""apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {app_name}
    app.kubernetes.io/part-of: {application["id"]}
    managed-by: luanvan-platform
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {app_name}
  minReplicas: {int(service.get("min_replicas", 1))}
  maxReplicas: {int(service.get("max_replicas", 3))}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {int(service.get("cpu_threshold", 70))}
"""
            )

    return "---\n".join(documents)


def write_manifest(application: dict[str, Any]) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = GENERATED_DIR / f"{application['id']}.yaml"
    manifest_path.write_text(build_manifest(application), encoding="utf-8")
    return manifest_path