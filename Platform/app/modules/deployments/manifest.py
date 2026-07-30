from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.secret_store import get_secret_checksum, get_secret_map, list_secret_keys

GENERATED_DIR = BASE_DIR / "k8s" / "generated"


def _yaml_value(value: str) -> str:
    escaped = str(value).replace('"', '\\"')
    return f'"{escaped}"'


def _validate_resource_quantity(value: str, field: str) -> str:
    v = str(value).strip()
    if not v:
        return ""
    import re
    if not re.fullmatch(r"\d+m?|\d+(\.\d+)?[GMk]?i?", v):
        raise ValueError(f"{field} không đúng định dạng Kubernetes resource quantity (vd: 100m, 256Mi).")
    return v


def _build_probes(service: dict[str, Any]) -> str:
    probes_yaml = ""

    for probe_type in ("startupProbe", "readinessProbe", "livenessProbe"):
        probe = service.get(probe_type)
        if not probe or not isinstance(probe, dict):
            continue
        probe_type_short = probe.get("type", "httpGet")
        if not probe_type_short:
            continue

        probe_yaml = f"          {probe_type}:\n"
        if probe_type_short == "httpGet":
            probe_yaml += f"            httpGet:\n"
            probe_yaml += f"              path: {_yaml_value(probe.get('path', '/'))}\n"
            probe_yaml += f"              port: {int(probe.get('port', service.get('container_port', 80)))}\n"
        elif probe_type_short == "tcpSocket":
            probe_yaml += f"            tcpSocket:\n"
            probe_yaml += f"              port: {int(probe.get('port', service.get('container_port', 80)))}\n"
        elif probe_type_short == "exec":
            command = probe.get("command", [])
            if command:
                probe_yaml += f"            exec:\n"
                probe_yaml += f"              command:\n"
                for cmd in command:
                    probe_yaml += f"                - {_yaml_value(str(cmd))}\n"

        if probe.get("initialDelaySeconds"):
            probe_yaml += f"            initialDelaySeconds: {int(probe['initialDelaySeconds'])}\n"
        if probe.get("periodSeconds"):
            probe_yaml += f"            periodSeconds: {int(probe['periodSeconds'])}\n"
        if probe.get("timeoutSeconds"):
            probe_yaml += f"            timeoutSeconds: {int(probe['timeoutSeconds'])}\n"
        if probe.get("failureThreshold"):
            probe_yaml += f"            failureThreshold: {int(probe['failureThreshold'])}\n"
        if probe.get("successThreshold"):
            probe_yaml += f"            successThreshold: {int(probe['successThreshold'])}\n"

        probes_yaml += probe_yaml

    return f"\n{probes_yaml}" if probes_yaml else ""


def _build_env_from(service: dict[str, Any], application: dict[str, Any]) -> str:
    env_yaml = ""
    env_items = service.get("env", [])
    config_refs = service.get("config_refs", [])
    secret_refs = service.get("secret_refs", [])

    if not env_items and not config_refs and not secret_refs:
        return env_yaml

    env_yaml = "\n          env:\n"
    for item in env_items:
        env_yaml += f"            - name: {item['name']}\n              value: {_yaml_value(str(item['value']))}\n"

    for ref in config_refs:
        if isinstance(ref, dict) and ref.get("name"):
            cm_name = ref.get("config_map_name", f"{application['id']}-config")
            env_yaml += f"            - name: {ref['name']}\n"
            env_yaml += f"              valueFrom:\n"
            env_yaml += f"                configMapKeyRef:\n"
            env_yaml += f"                  name: {cm_name}\n"
            env_yaml += f"                  key: {ref.get('key', ref['name'])}\n"

    for ref in secret_refs:
        if isinstance(ref, dict) and ref.get("name"):
            secret_name = ref.get("secret_name", f"{application['id']}-secret")
            env_yaml += f"            - name: {ref['name']}\n"
            env_yaml += f"              valueFrom:\n"
            env_yaml += f"                secretKeyRef:\n"
            env_yaml += f"                  name: {secret_name}\n"
            env_yaml += f"                  key: {ref.get('key', ref['name'])}\n"

    return env_yaml


def _build_storage(service: dict[str, Any]) -> tuple[str, str]:
    volume_mounts = ""
    volumes = ""
    storage = service.get("storage", [])
    if not storage or not isinstance(storage, list):
        return volume_mounts, volumes

    vm_lines: list[str] = []
    v_lines: list[str] = []
    for i, vol in enumerate(storage):
        vol_name = vol.get("name", f"data-{i}")
        mount_path = vol.get("mount_path", "/data")
        size = vol.get("size", "1Gi")
        sc = vol.get("storage_class", "")
        sc_yaml = f"\n  storageClassName: {sc}" if sc else ""

        vm_lines.append(f"            - name: {vol_name}\n              mountPath: {mount_path}")

        if vol.get("type") == "pvc":
            pvc_name = vol.get("pvc_name", f"{vol_name}-pvc")
            v_lines.append(
                f"        - name: {vol_name}\n"
                f"          persistentVolumeClaim:\n"
                f"            claimName: {pvc_name}"
            )
        elif vol.get("type") == "configMap":
            cm_name = vol.get("config_map_name", f"{vol_name}")
            v_lines.append(
                f"        - name: {vol_name}\n"
                f"          configMap:\n"
                f"            name: {cm_name}"
            )
        elif vol.get("type") == "secret":
            secret_name = vol.get("secret_name", f"{vol_name}")
            v_lines.append(
                f"        - name: {vol_name}\n"
                f"          secret:\n"
                f"            secretName: {secret_name}"
            )

    if vm_lines:
        volume_mounts = "\n          volumeMounts:\n" + "\n".join(vm_lines)
    if v_lines:
        volumes = "\n      volumes:\n" + "\n".join(v_lines)

    return volume_mounts, volumes


def _build_command_args(service: dict[str, Any]) -> str:
    command = service.get("command", [])
    args = service.get("args", [])
    lines = "\n" if command or args else ""

    if command:
        lines += "          command:\n"
        for c in command:
            lines += f"            - {_yaml_value(str(c))}\n"
    if args:
        lines += "          args:\n"
        for a in args:
            lines += f"            - {_yaml_value(str(a))}\n"

    return lines


def build_manifest(application: dict[str, Any]) -> str:
    namespace = application["namespace"]
    image_pull_secret_name = application.get("image_pull_secret", "")
    if image_pull_secret_name == "***":
        image_pull_secret_name = ""
    secret_checksum = get_secret_checksum(application["id"])
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

    # ResourceQuota
    resource_quota = application.get("resource_quota")
    if resource_quota and isinstance(resource_quota, dict):
        rq_lines: list[str] = []
        if resource_quota.get("cpu"):
            rq_lines.append(f"    requests.cpu: \"{resource_quota['cpu']}\"")
            rq_lines.append(f"    limits.cpu: \"{resource_quota['cpu']}\"")
        if resource_quota.get("memory"):
            rq_lines.append(f"    requests.memory: \"{resource_quota['memory']}\"")
            rq_lines.append(f"    limits.memory: \"{resource_quota['memory']}\"")
        if resource_quota.get("pods"):
            rq_lines.append(f"    pods: \"{int(resource_quota['pods'])}\"")
        if resource_quota.get("pvc"):
            rq_lines.append(f"    persistentvolumeclaims: \"{int(resource_quota['pvc'])}\"")
        if rq_lines:
            documents.append(
                f"""apiVersion: v1
kind: ResourceQuota
metadata:
  name: {application['id']}-quota
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
spec:
  hard:
""" + "\n".join(rq_lines) + "\n"
            )

    # LimitRange
    limit_range = application.get("limit_range")
    if limit_range and isinstance(limit_range, dict):
        lr_parts: list[str] = []
        if limit_range.get("default_cpu") or limit_range.get("default_memory"):
            lr_parts.append("      default:")
            if limit_range.get("default_cpu"):
                lr_parts.append(f"        cpu: \"{limit_range['default_cpu']}\"")
            if limit_range.get("default_memory"):
                lr_parts.append(f"        memory: \"{limit_range['default_memory']}\"")
        if limit_range.get("default_request_cpu") or limit_range.get("default_request_memory"):
            lr_parts.append("      defaultRequest:")
            if limit_range.get("default_request_cpu"):
                lr_parts.append(f"        cpu: \"{limit_range['default_request_cpu']}\"")
            if limit_range.get("default_request_memory"):
                lr_parts.append(f"        memory: \"{limit_range['default_request_memory']}\"")
        if lr_parts:
            documents.append(
                f"""apiVersion: v1
kind: LimitRange
metadata:
  name: {application['id']}-limits
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
spec:
  limits:
    - type: Container
""" + "\n".join(lr_parts) + "\n"
            )

    # ConfigMap
    config_data = application.get("config_data", {})
    if config_data:
        cm_lines: list[str] = []
        for k, v in config_data.items():
            cm_lines.append(f"  {k}: {_yaml_value(str(v))}")
        documents.append(
            f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {application['id']}-config
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
data:
""" + "\n".join(cm_lines) + "\n"
        )

    # Secret
    secret_map = get_secret_map(application["id"])
    if secret_map:
        secret_lines: list[str] = []
        for k, v in secret_map.items():
            import base64
            encoded = base64.b64encode(v.encode()).decode()
            secret_lines.append(f"  {k}: {encoded}")
        documents.append(
            f"""apiVersion: v1
kind: Secret
metadata:
  name: {application['id']}-secret
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
type: Opaque
data:
""" + "\n".join(secret_lines) + "\n"
        )

    # PVC declarations from services
    for service in application.get("services", []):
        storage = service.get("storage", [])
        if not storage:
            continue
        for vol in storage:
            if vol.get("type") != "pvc":
                continue
            pvc_name = vol.get("pvc_name", f"{vol.get('name', 'data')}-pvc")
            size = vol.get("size", "1Gi")
            sc = vol.get("storage_class", "")
            access_modes = vol.get("access_modes", ["ReadWriteOnce"])
            sc_yaml = f"  storageClassName: {sc}\n" if sc else ""
            retain_policy = vol.get("retain_policy", "Retain")

            documents.append(
                f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {pvc_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
    platform/retain-policy: {retain_policy}
  annotations:
    platform.luanvan/retain-policy: {retain_policy}
spec:
  accessModes:
""" + "\n".join(f"    - {am}" for am in access_modes) + f"""
{sc_yaml}  resources:
    requests:
      storage: {size}
"""
            )

    # Managed databases as StatefulSet
    databases = application.get("databases", [])
    for db in databases:
        db_name = db.get("name", "mysql")
        db_app_name = f"{application['id']}-{db_name}"
        db_image = db.get("image", "mysql:8.0")
        db_port = int(db.get("port", 3306))
        db_storage_size = db.get("storage_size", "1Gi")
        db_storage_class = db.get("storage_class", "")
        db_sc_yaml = f"  storageClassName: {db_storage_class}\n" if db_storage_class else ""
        db_password = db.get("password_key", "MYSQL_ROOT_PASSWORD")
        db_env_lines: list[str] = []
        for env_item in db.get("env", []):
            db_env_lines.append(f"            - name: {env_item['name']}\n              value: {_yaml_value(str(env_item['value']))}")
        if db_env_lines:
            db_env_lines.insert(0, "          env:")

        # Init SQL ConfigMap
        init_sql = db.get("init_sql")
        if init_sql:
            cm_name = f"{db_app_name}-init-sql"
            sql_basename = init_sql.get("name", "init.sql")
            sql_content = init_sql.get("content", "")
            indented_sql = "\n".join("    " + line for line in sql_content.splitlines())
            documents.append(
                f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {cm_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {db_app_name}
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
data:
  {sql_basename}: |
{indented_sql}
"""
            )

        # Init Job
        init_job = db.get("init_job")
        if init_job:
            job_image = init_job.get("image", db_image)
            job_command = init_job.get("command", ["echo", "init complete"])
            job_name = f"{db_app_name}-init"
            documents.append(
                f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {db_app_name}
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: init
          image: {job_image}
          command:
""" + "\n".join(f"            - {_yaml_value(str(c))}" for c in job_command) + f"""
          env:
            - name: DB_HOST
              value: {db_app_name}
            - name: DB_PORT
              value: "{db_port}"
          envFrom:
            - secretRef:
                name: {application['id']}-secret
"""
            )

        db_spec_prefix = ""
        if db.get("kind") == "StatefulSet":
            db_spec_prefix = "  serviceName: " + db_app_name + "\n"
        else:
            db_spec_prefix = ""

        db_init_sql_mount = ""
        if init_sql:
            db_init_sql_mount = f"""
            - name: init-sql
              mountPath: /docker-entrypoint-initdb.d
"""
        documents.append(
            f"""apiVersion: apps/v1
kind: {"StatefulSet" if db.get("kind") == "StatefulSet" else "Deployment"}
metadata:
  name: {db_app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {db_app_name}
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
spec:
{db_spec_prefix}  replicas: {int(db.get("replicas", 1))}
  selector:
    matchLabels:
      app.kubernetes.io/name: {db_app_name}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {db_app_name}
        app.kubernetes.io/part-of: {application['id']}
    spec:
      containers:
        - name: {db_name}
          image: {db_image}
          ports:
            - containerPort: {db_port}
{chr(10).join(db_env_lines)}
          volumeMounts:
            - name: {db_name}-data
              mountPath: /var/lib/mysql
{db_init_sql_mount}"""
        )

        # PVC for database
        if db.get("kind") == "StatefulSet":
            db_volume_templates = f"""  volumeClaimTemplates:
    - metadata:
        name: {db_name}-data
        labels:
          app.kubernetes.io/name: {db_app_name}
          app.kubernetes.io/part-of: {application['id']}
      spec:
        accessModes:
          - ReadWriteOnce
{db_sc_yaml}        resources:
          requests:
            storage: {db_storage_size}
"""
            documents[-1] += db_volume_templates
        else:
            documents.append(
                f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {db_app_name}-pvc
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {db_app_name}
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
spec:
  accessModes:
    - ReadWriteOnce
{db_sc_yaml}  resources:
    requests:
      storage: {db_storage_size}
"""
            )

        if init_sql:
            documents[-2] += f"""
      volumes:
        - name: {db_name}-data
          persistentVolumeClaim:
            claimName: {db_app_name}-pvc
        - name: init-sql
          configMap:
            name: {cm_name}"""

        # Service for database
        documents.append(
            f"""apiVersion: v1
kind: Service
metadata:
  name: {db_app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {db_app_name}
    app.kubernetes.io/part-of: {application['id']}
    managed-by: luanvan-platform
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {db_app_name}
  ports:
    - name: {db_name}
      port: {db_port}
      targetPort: {db_port}
"""
        )

    # ============================================================================
    # Services
    # ============================================================================
    for service in application.get("services", []):
        app_name = f"{application['id']}-{service['name']}"

        image = service.get("image", "")
        image_digest = service.get("image_digest", "")
        image_ref = f"{image}@{image_digest}" if image_digest else image

        env_yaml = _build_env_from(service, application)
        probes_yaml = _build_probes(service)
        volume_mounts, volumes = _build_storage(service)
        cmd_args = _build_command_args(service)

        resources_yaml = ""
        cpu_req = _validate_resource_quantity(service.get("cpu_request", "100m"), "cpu_request")
        cpu_lim = _validate_resource_quantity(service.get("cpu_limit", "500m"), "cpu_limit")
        mem_req = _validate_resource_quantity(service.get("memory_request", "128Mi"), "memory_request")
        mem_lim = _validate_resource_quantity(service.get("memory_limit", "512Mi"), "memory_limit")

        if cpu_req or cpu_lim or mem_req or mem_lim:
            resources_yaml = "\n          resources:"
            if cpu_req or mem_req:
                resources_yaml += "\n            requests:"
                if cpu_req:
                    resources_yaml += f"\n              cpu: {cpu_req}"
                if mem_req:
                    resources_yaml += f"\n              memory: {mem_req}"
            if cpu_lim or mem_lim:
                resources_yaml += "\n            limits:"
                if cpu_lim:
                    resources_yaml += f"\n              cpu: {cpu_lim}"
                if mem_lim:
                    resources_yaml += f"\n              memory: {mem_lim}"

        pull_secret_yaml = ""
        if image_pull_secret_name and image_ref:
            pull_secret_yaml = f"\n      imagePullSecrets:\n        - name: {image_pull_secret_name}"

        deploy_kind = "StatefulSet" if service.get("statefulset") else "Deployment"
        spec_prefix = ""
        if deploy_kind == "StatefulSet":
            spec_prefix = f"  serviceName: {app_name}\n"

        checksum_annotation = ""
        if secret_checksum:
            checksum_annotation = (
                "\n      annotations:"
                f"\n        platform.luanvan/secret-checksum: \"{secret_checksum}\""
            )

        documents.append(
            f"""apiVersion: apps/v1
kind: {deploy_kind}
metadata:
  name: {app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {app_name}
    app.kubernetes.io/part-of: {application["id"]}
    managed-by: luanvan-platform
spec:
{spec_prefix}  replicas: {int(service.get("replicas", 1))}
  selector:
    matchLabels:
      app.kubernetes.io/name: {app_name}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {app_name}
        app.kubernetes.io/part-of: {application["id"]}{checksum_annotation}
    spec:{pull_secret_yaml}
      containers:
        - name: {service["name"]}
          image: {image_ref}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: {int(service.get("container_port", 80))}{env_yaml}{resources_yaml}{cmd_args}{volume_mounts}{probes_yaml}{volumes}
"""
        )

        # Service exposure
        svc_type = service.get("service_type", "ClusterIP")
        is_public = service.get("public", False) or svc_type not in ("ClusterIP", "")
        effective_type = svc_type if is_public else "ClusterIP"

        node_port = service.get("node_port", "")
        node_port_yaml = ""
        if effective_type == "NodePort" and node_port:
            node_port_yaml = f"\n      nodePort: {node_port}"

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
  type: {effective_type}
  selector:
    app.kubernetes.io/name: {app_name}
  ports:
    - name: http
      port: {int(service.get("container_port", 80))}
      targetPort: {int(service.get("container_port", 80))}{node_port_yaml}
"""
        )

        # HPA
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
    kind: {deploy_kind}
    name: {app_name}
  minReplicas: {int(service.get("min_replicas", 2))}
  maxReplicas: {int(service.get("max_replicas", 5))}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {int(service.get("cpu_threshold", 50))}
"""
            )

        # Ingress for public web/API services
        ingress = service.get("ingress")
        if ingress and isinstance(ingress, dict) and is_public:
            ingress_host = ingress.get("host", "")
            ingress_path = ingress.get("path", "/")
            ingress_tls = ingress.get("tls", False)
            tls_yaml = ""
            if ingress_tls:
                tls_secret = ingress.get("tls_secret", f"{application['id']}-tls")
                tls_yaml = f"""
  tls:
    - hosts:
        - {ingress_host}
      secretName: {tls_secret}"""

            path_type = ingress.get("path_type", "Prefix")
            documents.append(
                f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {app_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {app_name}
    app.kubernetes.io/part-of: {application["id"]}
    managed-by: luanvan-platform
spec:{tls_yaml}
  rules:
    - host: {ingress_host}
      http:
        paths:
          - path: {ingress_path}
            pathType: {path_type}
            backend:
              service:
                name: {app_name}
                port:
                  number: {int(service.get("container_port", 80))}
"""
            )

    return "---\n".join(documents)


def write_manifest(application: dict[str, Any]) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = GENERATED_DIR / f"{application['id']}.yaml"
    manifest_path.write_text(build_manifest(application), encoding="utf-8")
    return manifest_path
