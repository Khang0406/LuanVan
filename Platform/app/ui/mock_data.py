from app.modules.applications.service import load_accessible_applications
from app.modules.servers.service import load_servers


def dashboard_stats(user):
    applications = load_accessible_applications(user)
    servers = load_servers() if user.is_admin else []
    master_count = sum(1 for server in servers if server.get("role") == "Master")
    worker_count = sum(1 for server in servers if server.get("role") == "Worker")

    from app.modules.pipeline.engine import load_pipeline_runs

    application_ids = {app["id"] for app in applications}
    running_jobs = sum(
        1
        for run in load_pipeline_runs()
        if run.get("application_id") in application_ids and run.get("status") == "Running"
    )

    return {
        "servers": len(servers),
        "clusters": 1 if servers else 0,
        "nodes_ready": f"{master_count} master / {worker_count} worker",
        "applications": len(applications),
        "deployments": sum(1 for app in applications if app.get("status") in {"Running", "Deployed"}),
        "jobs_running": running_jobs,
    }

SERVERS = [
    {"name": "mgmt-web", "ip": "172.30.122.42", "role": "Management", "status": "Online", "cpu": "22%", "ram": "3.1/8 GB"},
    {"name": "k8s-master-01", "ip": "172.30.122.51", "role": "Master", "status": "Ready", "cpu": "35%", "ram": "4.8/16 GB"},
    {"name": "k8s-worker-01", "ip": "172.30.122.61", "role": "Worker", "status": "Ready", "cpu": "41%", "ram": "6.2/16 GB"},
    {"name": "k8s-worker-02", "ip": "172.30.122.62", "role": "Worker", "status": "Ready", "cpu": "28%", "ram": "5.4/16 GB"},
]

INSTALL_STEPS = [
    {"step": "Kiểm tra SSH", "status": "Hoàn tất", "note": "Kiểm tra SSH/Ansible ping trên các server đã chọn"},
    {"step": "Cài gói nền", "status": "Hoàn tất", "note": "curl, container runtime, sysctl"},
    {"step": "Cài master", "status": "Đang chạy", "note": "Khởi tạo K3s control plane"},
    {"step": "Join workers", "status": "Chờ", "note": "Sử dụng node-token từ master"},
    {"step": "Kiểm tra cluster", "status": "Chờ", "note": "kubectl get nodes"},
]

APPLICATIONS = [
    {"name": "student-portal", "owner": "team-web", "namespace": "prod-student", "status": "Running", "replicas": "4/4", "url": "https://student.demo.local"},
    {"name": "admin-api", "owner": "team-backend", "namespace": "prod-admin", "status": "Running", "replicas": "3/3", "url": "https://api.demo.local"},
    {"name": "landing-page", "owner": "team-web", "namespace": "staging-web", "status": "Deploying", "replicas": "1/2", "url": "https://landing.demo.local"},
]

PIPELINE_EVENTS = [
    {"time": "09:10", "actor": "Developer", "event": "Push code lên GitHub", "status": "Done"},
    {"time": "09:11", "actor": "GitHub Actions", "event": "Build Docker image", "status": "Done"},
    {"time": "09:14", "actor": "Platform", "event": "Redeploy lên Kubernetes", "status": "Running"},
]

AUDIT_LOGS = [
    {"user": "admin", "action": "INSTALL_CLUSTER", "target": "lab-cluster", "result": "SUCCESS", "time": "08:30"},
    {"user": "khang", "action": "DEPLOY_SERVICE", "target": "student-portal", "result": "SUCCESS", "time": "09:14"},
    {"user": "operator", "action": "SCALE_REPLICA", "target": "admin-api", "result": "SUCCESS", "time": "09:20"},
]
