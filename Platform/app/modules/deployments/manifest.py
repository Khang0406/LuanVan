from pathlib import Path


def safe_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(" ", "-")


def render_service_manifest(service, output_dir: Path) -> Path:
    app = service.application
    namespace = safe_name(app.namespace)
    name = safe_name(service.name)
    node_port_line = f"      nodePort: {service.node_port}\n" if service.node_port else ""
    manifest = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: {service.replicas}
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: {name}
          image: {service.image}
          ports:
            - containerPort: {service.port}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {namespace}
spec:
  type: {service.service_type}
  selector:
    app: {name}
  ports:
    - port: {service.port}
      targetPort: {service.port}
{node_port_line}"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"service_{service.id}.yaml"
    path.write_text(manifest, encoding="utf-8")
    return path
