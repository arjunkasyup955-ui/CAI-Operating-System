from deployment.targets import DeploymentProfile, resolve_profile_defaults


def generate_k8s_deployment(
    project_name: str,
    image: str,
    port: int = 8000,
    health_path: str = "/health",
    profile: str = DeploymentProfile.PRODUCTION,
    env_vars: list[str] | None = None,
) -> dict:
    defaults = resolve_profile_defaults(profile)
    env = [{"name": key, "valueFrom": {"secretKeyRef": {"name": f"{project_name}-secrets", "key": key}}} for key in (env_vars or [])]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": project_name, "labels": {"app": project_name}},
        "spec": {
            "replicas": defaults["replicas"],
            "selector": {"matchLabels": {"app": project_name}},
            "template": {
                "metadata": {"labels": {"app": project_name}},
                "spec": {
                    "containers": [
                        {
                            "name": project_name,
                            "image": image,
                            "ports": [{"containerPort": port}],
                            "env": env,
                            "resources": {
                                "requests": {"cpu": defaults["cpu"], "memory": defaults["memory"]},
                                "limits": {"cpu": defaults["cpu"], "memory": defaults["memory"]},
                            },
                            "livenessProbe": {"httpGet": {"path": health_path, "port": port}, "initialDelaySeconds": 10, "periodSeconds": 30},
                            "readinessProbe": {"httpGet": {"path": health_path, "port": port}, "initialDelaySeconds": 5, "periodSeconds": 10},
                        }
                    ]
                },
            },
        },
    }


def generate_k8s_service(project_name: str, port: int = 80, target_port: int = 8000) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": project_name},
        "spec": {
            "selector": {"app": project_name},
            "ports": [{"port": port, "targetPort": target_port}],
            "type": "ClusterIP",
        },
    }


def generate_k8s_ingress(project_name: str, host: str, service_port: int = 80) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": project_name, "annotations": {"nginx.ingress.kubernetes.io/rewrite-target": "/"}},
        "spec": {
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {"path": "/", "pathType": "Prefix", "backend": {"service": {"name": project_name, "port": {"number": service_port}}}}
                        ]
                    },
                }
            ]
        },
    }


def generate_k8s_secrets_template(project_name: str, secret_keys: list[str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"{project_name}-secrets"},
        "type": "Opaque",
        "stringData": {key: "" for key in secret_keys},
    }
