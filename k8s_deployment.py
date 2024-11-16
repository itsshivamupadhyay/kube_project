import argparse
import subprocess
import yaml
import json
import os
import sys
import time
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def get_deployment_health_status(deployment_uid, namespace):
    config.load_kube_config()
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()

    # Step 1: Retrieve the Deployment and Pod Status
    try:
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
        deployment = next((d for d in deployments.items if d.metadata.uid == deployment_uid), None)
        
        if not deployment:
            return {"error": f"Deployment with UID {deployment_uid} not found in namespace {namespace}."}
        
        deployment_status = {
            "name": deployment.metadata.name,
            "replicas": deployment.status.replicas,
            "available_replicas": deployment.status.available_replicas,
            "unavailable_replicas": deployment.status.unavailable_replicas,
            "conditions": [c.to_dict() for c in deployment.status.conditions]
        }

        # Step 2: Retrieve CPU and Memory Metrics for the Deployment's Pods

        pod_metrics = []
        try:
            pod_list = core_v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment.metadata.name}")
            for pod in pod_list.items:
                pod_name = pod.metadata.name
                pod_status = pod.status.phase

                # Retrieve CPU and memory usage
                cpu_usage, memory_usage = None, None
                try:
                    result = subprocess.check_output(f"kubectl top pod {pod_name} -n {namespace} --no-headers", shell=True)
                    result = result.decode("utf-8").strip()
                    if result:
                        metrics = result.split()
                        cpu_usage, memory_usage = metrics[1], metrics[2]
                except subprocess.CalledProcessError as e:
                    cpu_usage, memory_usage = "Metrics unavailable", "Metrics unavailable"

                pod_metrics.append({
                    "pod_name": pod_name,
                    "status": pod_status,
                    "cpu_usage": cpu_usage,
                    "memory_usage": memory_usage
                })

        except ApiException as e:
            print(f"Error retrieving pod metrics: {str(e)}")
        
        # Step 3: Check for Any Issues or Failures in Deployment
        issues = []
        for condition in deployment.status.conditions:
            if condition.status != "True":
                issues.append({
                    "type": condition.type,
                    "reason": condition.reason,
                    "message": condition.message
                })
        
        # Step 4: Compile Health Status Report

        health_status = {
            "deployment_status": deployment_status,
            "pod_metrics": pod_metrics,
            "issues": issues if issues else "No issues detected"
        }
        
        return health_status
    
    except ApiException as e:
        raise Exception(f"Exception when retrieving deployment health status: {str(e)}")

def create_namespace_if_not_exists(namespace):
    """Check if a namespace exists and create it if not."""
    core_v1 = client.CoreV1Api()
    try:
        # Check if the namespace exists
        core_v1.read_namespace(name=namespace)
        print(f"Namespace '{namespace}' already exists.")
    except ApiException as e:
        if e.status == 404:
            # Namespace not found, create it
            print(f"Namespace '{namespace}' does not exist. Creating namespace.")
            namespace_body = client.V1Namespace(
                metadata=client.V1ObjectMeta(name=namespace)
            )
            core_v1.create_namespace(body=namespace_body)
            print(f"Namespace '{namespace}' created successfully.")
        else:
            raise Exception(f"Error checking namespace: {str(e)}")
        
def install_metrics_server():
    print("Installing Metrics Server...")
    
    # Check if metrics server already exists
    try:
        subprocess.check_output("kubectl get deployment metrics-server -n kube-system", shell=True)
        print("Metrics Server already exists. Patching deployment with updated configuration.")
        # If it exists, patch the deployment with the insecure TLS flag
        patch_metrics_server()
    except subprocess.CalledProcessError:
        # If it doesn't exist, apply the Metrics Server YAML
        print("Metrics Server not found. Installing...")
        os.system("kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml")
        # Wait for the newly deployed pod to be ready
        wait_for_pod_to_be_ready("metrics-server", "kube-system")
        # After it's ready, patch it with the necessary configuration
        patch_metrics_server()

def patch_metrics_server():
    print("Patching Metrics Server with --kubelet-insecure-tls...")
    os.system("kubectl patch deployment metrics-server -n kube-system --type='json' -p='[{\"op\": \"add\", \"path\": \"/spec/template/spec/containers/0/args/-\", \"value\": \"--kubelet-insecure-tls\"}]'")

    # Delete old metrics-server pods to make sure the new one is used
    print("Deleting old metrics-server pods to trigger recreation with new config...")
    os.system("kubectl delete pod -n kube-system -l k8s-app=metrics-server")

    # Wait for the new pod to be running
    wait_for_pod_to_be_ready("metrics-server", "kube-system")
    print("Metrics Server has been patched and is now running with the updated configuration.")

def verify_metrics_server():
    print("Verifying Metrics Server installation...")
    os.system("kubectl get deployment metrics-server -n kube-system")

def check_metrics_availability():
    print("Checking metrics availability...")
    os.system("kubectl top nodes")
    os.system("kubectl top pods")

def wait_for_pod_to_be_ready(pod_name, namespace):
    print(f"Waiting for pod '{pod_name}' in namespace '{namespace}' to be ready...")
    try:
        # Wait for the pod to be in a 'Ready' state
        subprocess.check_call(
            f"kubectl wait --for=condition=ready pod -l app={pod_name} -n {namespace} --timeout=300s",
            shell=True
        )
        print(f"Pod '{pod_name}' is now Ready.")
    except subprocess.CalledProcessError as e:
        print(f"Error waiting for pod '{pod_name}' to be ready: {e}")


def check_hpa_ready(hpa_name, namespace):
    # Initialize the Autoscaling V2 API client
    autoscaling_v2 = client.AutoscalingV2Api()

    try:
        # Retrieve the HPA object
        hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name=hpa_name, namespace=namespace)
        
        # Check if conditions are available
        if hpa.status.conditions is None:
            return False  # No conditions, so the HPA is not ready yet
        
        # Check if the 'AbleToScale' condition is True
        for condition in hpa.status.conditions:
            if condition.type == "AbleToScale" and condition.status == "True":
                return True  # HPA is ready for scaling
        
        # If the condition is not found or not True, return False
        return False

    except ApiException as e:
        raise Exception(f"Exception when retrieving HPA status: {str(e)}")

def get_hpa_status(hpa_name, namespace):
    # Wait until the HPA is ready
    while not check_hpa_ready(hpa_name, namespace):
        print(f"HPA '{hpa_name}' in namespace '{namespace}' is not ready, waiting...")
        time.sleep(5)  # Wait for 5 seconds before rechecking

    # Initialize the Autoscaling V2 API client
    autoscaling_v2 = client.AutoscalingV2Api()

    try:
        # Retrieve the HPA object
        hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name=hpa_name, namespace=namespace)
        
        # Extract relevant details from the HPA status
        status = {
            "current_replicas": hpa.status.current_replicas,  # The current number of replicas
            "desired_replicas": hpa.status.desired_replicas,  # The desired number of replicas
            "current_metrics": hpa.status.current_metrics       # Metrics used for scaling, like CPU and memory utilization
        }
        
        return status
    
    except ApiException as e:
        raise Exception(f"Exception when retrieving HPA status: {str(e)}")
def get_create_command_hpa_status(hpa_name, namespace):
    autoscaling_v2 = client.AutoscalingV2Api()
    try:
        hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name=hpa_name, namespace=namespace)
        status = {
            "current_replicas": hpa.status.current_replicas,
            "desired_replicas": hpa.status.desired_replicas,
            "current_metrics": hpa.status.current_metrics
        }
        return status
    except ApiException as e:
        raise Exception(f"Exception when retrieving HPA status: {str(e)}")

def wait_for_metrics_availability():
    print("Waiting for metrics availability...")

    while True:
        try:
            # Check if metrics are available by running `kubectl top nodes`
            result = subprocess.check_output("kubectl top nodes", shell=True, stderr=subprocess.STDOUT)
            print("Metrics are available.")
            break
        except subprocess.CalledProcessError as e:
            # If metrics are not available, an error will occur
            if "Metrics API not available" in e.output.decode():
                print("Metrics API not available, retrying...")
                time.sleep(5)
            else:
                print(f"Error checking metrics availability: {e.output.decode()}")
                break


def run_command(command):
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Command failed: {command}\n{result.stderr}")
    return result.stdout

def connect_to_cluster():
    try:
        config.load_kube_config()
        print("Connected to Kubernetes cluster")
    except Exception as e:
        raise Exception(f"Failed to connect to Kubernetes cluster: {str(e)}")

def install_helm():
    try:
        run_command("curl https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-3 | bash")
        print("Helm installed successfully")
    except Exception as e:
        raise Exception(f"Failed to install Helm: {str(e)}")

def install_keda():
    try:
        run_command("helm repo add kedacore https://kedacore.github.io/charts")
        run_command("helm repo update")
        try:
            run_command("helm install keda kedacore/keda --namespace keda --create-namespace")
        except Exception as e:
            if "cannot re-use a name that is still in use" in str(e):
                run_command("helm upgrade keda kedacore/keda --namespace keda")
            else:
                raise e
        print("KEDA installed successfully")
    except Exception as e:
        raise Exception(f"Failed to install KEDA: {str(e)}")

def create_deployment(deployment_config, namespace):
    try:
        apps_v1 = client.AppsV1Api()
        core_v1 = client.CoreV1Api()
        autoscaling_v2 = client.AutoscalingV2Api()
        create_namespace_if_not_exists(namespace)

        # Create Deployment
        deployment = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(name=deployment_config['name']),
            spec=client.V1DeploymentSpec(
                replicas=deployment_config['replicas'],
                selector={'matchLabels': {'app': deployment_config['name']}},
                template=client.V1PodTemplateSpec(
                    metadata={'labels': {'app': deployment_config['name']}},
                    spec=client.V1PodSpec(
                        containers=[client.V1Container(
                            name=deployment_config['name'],
                            image=deployment_config['image'],
                            ports=[client.V1ContainerPort(container_port=port) for port in deployment_config['ports']],
                            resources=client.V1ResourceRequirements(
                                requests={'cpu': deployment_config['cpu_request'], 'memory': deployment_config['memory_request']},
                                limits={'cpu': deployment_config['cpu_limit'], 'memory': deployment_config['memory_limit']}
                            )
                        )]
                    )
                )
            )
        )
        try:
            apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment)
        except ApiException as e:
            if e.status == 409:
                apps_v1.replace_namespaced_deployment(name=deployment_config['name'], namespace=namespace, body=deployment)
            else:
                raise e
        print(f"Deployment {deployment_config['name']} created successfully")

        # Create Service
        service = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(name=deployment_config['name']),
            spec=client.V1ServiceSpec(
                selector={'app': deployment_config['name']},
                ports=[
                    client.V1ServicePort(name="http", port=80, target_port=80),
                    client.V1ServicePort(name="https", port=443, target_port=443)
                ]
            )
        )
        try:
            core_v1.create_namespaced_service(namespace=namespace, body=service)
        except ApiException as e:
            if e.status == 409:
                core_v1.replace_namespaced_service(name=deployment_config['name'], namespace=namespace, body=service)
            else:
                raise e
        print(f"Service {deployment_config['name']} created successfully")

        # Create HPA
        install_metrics_server()
        verify_metrics_server()
        wait_for_metrics_availability()
        check_metrics_availability()

        hpa = client.V2HorizontalPodAutoscaler(
            api_version="autoscaling/v2",
            kind="HorizontalPodAutoscaler",
            metadata=client.V1ObjectMeta(name=deployment_config['name']),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version="apps/v1",
                    kind="Deployment",
                    name=deployment_config['name']
                ),
                min_replicas=deployment_config['min_replicas'],
                max_replicas=deployment_config['max_replicas'],
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization",
                                average_utilization=deployment_config['cpu_target']
                            )
                        )
                    ),
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="memory",
                            target=client.V2MetricTarget(
                                type="Utilization",
                                average_utilization=deployment_config['memory_target']
                            )
                        )
                    )
                ]
            )
        )
        try:
            autoscaling_v2.create_namespaced_horizontal_pod_autoscaler(namespace=namespace, body=hpa)
        except ApiException as e:
            if e.status == 409:
                autoscaling_v2.replace_namespaced_horizontal_pod_autoscaler(name=deployment_config['name'], namespace=namespace, body=hpa)
            else:
                raise e
        print(f"HPA for {deployment_config['name']} created successfully with CPU and memory thresholds")

        # Wait for the deployment pods to come up
        wait_for_pod_to_be_ready(deployment_config['name'], namespace)

        # Retrieve and return deployment details (including service and HPA)
        deployment_status = get_deployment_status(deployment_config['name'], namespace)
        service_details = get_service_details(deployment_config['name'], namespace)
        hpa_status = get_create_command_hpa_status(deployment_config['name'], namespace)
        hpa_condition = get_hpa_status(deployment_config['name'], namespace)
        # Return deployment details
        print("Deployment: ", deployment_status)
        print("Service: ", service_details)
        print("HPA: ", hpa_status)
        print("HPA: ", hpa_condition)

    except ApiException as e:
        raise Exception(f"Exception when creating deployment: {str(e)}")
def get_service_details(service_name, namespace):
    core_v1 = client.CoreV1Api()
    try:
        service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        service_details = {
            "name": service.metadata.name,
            "type": service.spec.type,
            "cluster_ip": service.spec.cluster_ip,
            "ports": [{"port": port.port, "target_port": port.target_port} for port in service.spec.ports]
        }
        return service_details
    except ApiException as e:
        raise Exception(f"Exception when retrieving service details: {str(e)}")

def get_deployment_status(deployment_name, namespace):
    apps_v1 = client.AppsV1Api()
    try:
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        status = {
            "deployment_id": deployment.metadata.uid,
            "replicas": deployment.status.replicas,
            "available_replicas": deployment.status.available_replicas,
            "unavailable_replicas": deployment.status.unavailable_replicas
        }
        return status
    except ApiException as e:
        raise Exception(f"Exception when retrieving deployment status: {str(e)}")

def main():
    parser = argparse.ArgumentParser(description="Kubernetes Deployment Script")
    parser.add_argument("--create-deployment", action="store_true", help="Create Kubernetes deployment")
    parser.add_argument("--namespace", type=str, required=True, help="Namespace for deployment")
    parser.add_argument("--get-details-by-id", type=str, help="Retrieve deployment details by deployment UID")
    parser.add_argument("--image", type=str, help="Docker image for deployment")
    parser.add_argument("--deployment-name", type=str, help="Deployment name")
    parser.add_argument("--replicas", type=int, default=1, help="Number of replicas")
    parser.add_argument("--ports", type=int, nargs='+', default=[80], help="Ports for the deployment")
    parser.add_argument("--cpu-request", type=str, default="10m", help="CPU request for the containers")
    parser.add_argument("--memory-request", type=str, default="64Mi", help="Memory request for the containers")
    parser.add_argument("--cpu-limit", type=str, default="100m", help="CPU limit for the containers")
    parser.add_argument("--memory-limit", type=str, default="128Mi", help="Memory limit for the containers")
    parser.add_argument("--min-replicas", type=int, default=1, help="Minimum replicas for autoscaler")
    parser.add_argument("--max-replicas", type=int, default=3, help="Maximum replicas for autoscaler")
    parser.add_argument("--cpu-target", type=int, default=80, help="CPU target utilization for HPA")
    parser.add_argument("--memory-target", type=int, default=80, help="Memory target utilization for HPA")

    args = parser.parse_args()

    try:
        connect_to_cluster()
        if args.get_details_by_id:
            # Fetch and print deployment health status by UID
            health_status = get_deployment_health_status(args.get_details_by_id, args.namespace)
            print(json.dumps(health_status, indent=2,sort_keys=True, default=str))

        elif args.create_deployment:
            deployment_config = {
                "name": args.deployment_name,
                "image": args.image,
                "replicas": args.replicas,
                "ports": args.ports,
                "cpu_request": args.cpu_request,
                "memory_request": args.memory_request,
                "cpu_limit": args.cpu_limit,
                "memory_limit": args.memory_limit,
                "min_replicas": args.min_replicas,
                "max_replicas": args.max_replicas,
                "cpu_target": args.cpu_target,
                "memory_target": args.memory_target
            }
            create_deployment(deployment_config, args.namespace)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()