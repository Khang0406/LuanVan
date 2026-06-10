# Hướng dẫn chuẩn bị môi trường

## Management server

```bash
sudo apt update
sudo apt install -y git curl wget openssh-client sshpass python3 python3-venv python3-pip ansible
```

Cài kubectl và chuẩn bị kubeconfig sau khi K3s cài xong.

## Target servers

Mỗi máy Ubuntu target cần bật SSH:

```bash
sudo apt update
sudo apt install -y openssh-server curl
sudo systemctl enable --now ssh
```

Nên cấu hình SSH key từ management server:

```bash
ssh-copy-id ubuntu@<target-ip>
```
