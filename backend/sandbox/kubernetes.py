"""KubernetesProvider — manages sandbox Pods on a K8s cluster (GKE)."""
import asyncio
import re
import secrets
from datetime import datetime, timezone

import httpx
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

from core.config import get_config
from core.log import create_logger
from models.container import ContainerInfo, ContainerStatus
from sandbox.provider import SandboxProvider, stable_resource_alias

log = create_logger("sandbox.kubernetes")


def _safe_id(raw: str) -> str:
    """Convert a user/project id into a K8s-safe DNS label (lowercase, alnum + dash, max 63)."""
    s = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:63] or "default"


def _resource_id(user_id: str) -> str:
    """Build the legacy-stable resource suffix for user-level K8s objects.

    Existing PVC names are durable user data, so this identifier cannot be
    renamed in place. Pod/PVC/Service ownership checks below turn the old
    lossy normalization into fail-closed behavior instead of cross-tenant
    reuse. New Docker names can use a hash because their persistent volumes
    are keyed separately by the raw user id.
    """
    if _can_adopt_legacy_pvc(user_id):
        return _safe_id(user_id)
    return stable_resource_alias(user_id, max_length=53)


def _can_adopt_legacy_pvc(user_id: str) -> bool:
    """Whether an ownerless pre-migration PVC name maps uniquely to this id."""
    return user_id == "default" or bool(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", user_id))


class KubernetesProvider(SandboxProvider):
    supports_build = False

    def __init__(self):
        self.config = get_config()
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        self.core_api = k8s_client.CoreV1Api()
        self.namespace = self.config.k8s_namespace
        self._containers: dict[str, ContainerInfo] = {}
        self._api_keys: dict[str, str] = {}
        self._container_owners: dict[str, str] = {}
        self._container_projects: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def create_container(
        self, name: str, image: str | None = None, project_id: str | None = None, user_id: str | None = None,
    ) -> ContainerInfo:
        image = image or self.config.sandbox_image
        resolved_user_id = user_id or name
        rid = _resource_id(resolved_user_id)
        pod_name = f"sandbox-{rid}"
        svc_name = pod_name
        pvc_name = f"workspace-{rid}"
        api_key = secrets.token_urlsafe(32)
        loop = asyncio.get_event_loop()

        existing_pod = await self._find_existing_pod(resolved_user_id, loop)
        if existing_pod is not None:
            pod_name = existing_pod.metadata.name
            svc_name = pod_name
            rid = self._pod_resource_id(existing_pod)
            pvc_name = self._workspace_pvc_name(existing_pod) or pvc_name

        # Check if pod already exists and is running
        try:
            pod = await loop.run_in_executor(
                None, lambda: self.core_api.read_namespaced_pod(pod_name, self.namespace),
            )
            raw_pod_owner = self._pod_annotations(pod).get(
                "openbox.dev/user-id-raw"
            )
            legacy_owner_match = (
                not raw_pod_owner
                and _can_adopt_legacy_pvc(resolved_user_id)
                and self._pod_labels(pod).get("openbox.dev/user-id")
                == _safe_id(resolved_user_id)
            )
            if (
                (raw_pod_owner and raw_pod_owner != resolved_user_id)
                or (not raw_pod_owner and not legacy_owner_match)
            ):
                # Never reuse or delete a deterministic-name collision. The
                # durable PVC name stays backward compatible, so ownership
                # metadata (or a canonical generated id) is the safety fence.
                raise PermissionError(
                    f"Pod {pod_name} belongs to a different sandbox owner"
                )
            if legacy_owner_match:
                await self._adopt_pod_owner(
                    pod_name,
                    resolved_user_id,
                    loop,
                )
            await self._ensure_pvc(
                self._workspace_pvc_name(pod) or pvc_name,
                resolved_user_id,
                loop,
                allow_unowned_adoption=True,
            )
            if pod.status.phase == "Running":
                await self._ensure_service(
                    svc_name,
                    selector=self._service_selector_from_pod(pod),
                    loop=loop,
                )
                info = self._container_info_from_pod(
                    pod,
                    name=self._pod_display_name(pod) or name,
                    api_key=self._extract_api_key(pod),
                    user_id=resolved_user_id,
                )
                self._store_container_state(info, user_id=resolved_user_id)
                await self._wait_action_server(info.host, info.port)
                return info
            await loop.run_in_executor(
                None, lambda: self.core_api.delete_namespaced_pod(pod_name, self.namespace),
            )
            await self._wait_pod_deleted(pod_name, loop)
        except ApiException as e:
            if e.status != 404:
                raise

        # Ensure PVC
        await self._ensure_pvc(
            pvc_name,
            resolved_user_id,
            loop,
            allow_unowned_adoption=_can_adopt_legacy_pvc(resolved_user_id),
        )

        # Create Pod
        labels = {
            "app": "openbox-sandbox",
            "openbox.dev/resource-id": rid,
            "openbox.dev/user-id": _safe_id(resolved_user_id),
        }
        annotations = {
            "openbox.dev/user-id-raw": resolved_user_id,
            "openbox.dev/display-name": name,
        }

        sa_name = self.config.k8s_sandbox_service_account or None

        pod_manifest = k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name=pod_name, labels=labels, annotations=annotations),
            spec=k8s_client.V1PodSpec(
                service_account_name=sa_name,
                priority_class_name="sandbox-priority",
                volumes=[
                    k8s_client.V1Volume(
                        name="workspace",
                        persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_name),
                    ),
                    k8s_client.V1Volume(
                        name="dshm",
                        empty_dir=k8s_client.V1EmptyDirVolumeSource(medium="Memory", size_limit="256Mi"),
                    ),
                    k8s_client.V1Volume(
                        name="data",
                        empty_dir=k8s_client.V1EmptyDirVolumeSource(),
                    ),
                ],
                containers=[
                    k8s_client.V1Container(
                        name="sandbox",
                        image=image,
                        ports=[k8s_client.V1ContainerPort(container_port=8000)],
                        env=[k8s_client.V1EnvVar(name="SESSION_API_KEY", value=api_key)],
                        volume_mounts=[
                            k8s_client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                            k8s_client.V1VolumeMount(name="dshm", mount_path="/dev/shm"),
                            k8s_client.V1VolumeMount(name="data", mount_path="/data"),
                        ],
                        resources=k8s_client.V1ResourceRequirements(
                            requests={
                                "cpu": self.config.k8s_sandbox_cpu_request,
                                "memory": self.config.k8s_sandbox_memory_request,
                            },
                            limits={
                                "cpu": self.config.k8s_sandbox_cpu_limit,
                                "memory": self.config.k8s_sandbox_memory_limit,
                            },
                        ),
                        readiness_probe=k8s_client.V1Probe(
                            http_get=k8s_client.V1HTTPGetAction(path="/alive", port=8000),
                            initial_delay_seconds=2,
                            period_seconds=3,
                        ),
                        liveness_probe=k8s_client.V1Probe(
                            http_get=k8s_client.V1HTTPGetAction(path="/alive", port=8000),
                            initial_delay_seconds=10,
                            period_seconds=30,
                        ),
                    ),
                ],
            ),
        )

        created_here = True
        try:
            await loop.run_in_executor(
                None, lambda: self.core_api.create_namespaced_pod(self.namespace, pod_manifest),
            )
        except ApiException as e:
            if e.status == 409:
                # Another API/worker replica won the deterministic pod-name
                # race. Its SESSION_API_KEY is authoritative; keeping the
                # random key generated by this loser would cache a client that
                # can never authenticate to the reused action server.
                created_here = False
                existing_pod = await loop.run_in_executor(
                    None,
                    lambda: self.core_api.read_namespaced_pod(pod_name, self.namespace),
                )
                if self._pod_user_id(existing_pod) != resolved_user_id:
                    raise PermissionError(
                        f"Pod {pod_name} belongs to a different sandbox owner"
                    )
                api_key = self._extract_api_key(existing_pod)
                log.info(f"Pod {pod_name} already exists, reusing its credentials")
            else:
                raise

        # Ensure Service
        await self._ensure_service(svc_name, labels, loop)

        # Wait for pod ready
        await self._wait_pod_ready(pod_name, loop)

        host = f"{svc_name}.{self.namespace}.svc.cluster.local"
        info = ContainerInfo(
            id=pod_name,
            name=name,
            status=ContainerStatus.RUNNING,
            image=image,
            created_at=datetime.now(timezone.utc),
            host=host,
            port=8000,
            api_key=api_key,
        )

        self._store_container_state(info, user_id=resolved_user_id)

        # Wait for action server alive
        await self._wait_action_server(host, 8000)

        # Only the creator owns first-boot restoration. A losing replica may
        # arrive while that restore is already running and must not duplicate
        # it merely because create returned 409.
        if created_here:
            await self._trigger_restore(info)

        log.info(
            f"Sandbox pod {pod_name} {'created' if created_here else 'reused'}, host={host}"
        )
        return info

    async def delete_container(self, container_id: str, user_id: str | None = None) -> None:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)

        # Trigger backup before deletion
        await self._trigger_backup(info)

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self.core_api.delete_namespaced_pod(container_id, self.namespace),
            )
        except ApiException as e:
            if e.status != 404:
                raise

        try:
            await loop.run_in_executor(
                None, lambda: self.core_api.delete_namespaced_service(container_id, self.namespace),
            )
        except ApiException as e:
            if e.status != 404:
                raise

        # PVC is NOT deleted (preserve data)
        self._containers.pop(container_id, None)
        self._api_keys.pop(container_id, None)
        self._container_owners.pop(container_id, None)
        self._container_projects.pop(container_id, None)
        log.info(f"Sandbox pod {container_id} deleted (PVC preserved)")

    async def stop_container(self, container_id: str, user_id: str | None = None) -> None:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)

        await self._trigger_backup(info)

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self.core_api.delete_namespaced_pod(container_id, self.namespace),
            )
        except ApiException as e:
            if e.status != 404:
                raise

        info.status = ContainerStatus.STOPPED
        log.info(f"Sandbox pod {container_id} stopped (Pod deleted, Service+PVC preserved)")

    async def start_container(self, container_id: str, user_id: str | None = None) -> None:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)

        # Re-create the pod using stored info
        new_info = await self.create_container(
            name=info.name,
            image=info.image,
            project_id=None,
            user_id=self._container_owners.get(container_id),
        )
        info.status = new_info.status
        info.host = new_info.host
        info.port = new_info.port
        info.api_key = new_info.api_key

    async def get_container(self, container_id: str, user_id: str | None = None) -> ContainerInfo:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        return info

    async def list_containers(self) -> list[ContainerInfo]:
        return list(self._containers.values())

    async def forward_to_container(
        self, container_id: str, method: str, path: str, user_id: str | None = None, **kwargs,
    ) -> httpx.Response:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        if info.status != ContainerStatus.RUNNING:
            raise ValueError(f"Container {container_id} is not running")

        api_key = self._api_keys.get(container_id, "")
        url = f"http://{info.host}:{info.port}{path}"
        headers = kwargs.pop("headers", {})
        headers["X-API-Key"] = api_key

        timeout = kwargs.pop("timeout", 35.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, headers=headers, **kwargs)

    async def reconcile(self) -> None:
        """Restore in-memory state from existing K8s pods."""
        loop = asyncio.get_event_loop()
        try:
            pods = await loop.run_in_executor(
                None,
                lambda: self.core_api.list_namespaced_pod(
                    self.namespace, label_selector="app=openbox-sandbox",
                ),
            )
        except ApiException as exc:
            log.error(
                f"Failed to list pods during reconcile: {type(exc).__name__}"
            )
            return

        for pod in pods.items:
            pod_name = pod.metadata.name
            if pod_name in self._containers:
                continue

            raw_owner = self._pod_annotations(pod).get("openbox.dev/user-id-raw")
            if not raw_owner:
                # A normalized label is not an identity. A canonical legacy
                # tenant can adopt this pod on its next acquire, where the raw
                # authenticated id is available; startup reconciliation cannot
                # safely guess it.
                log.warning(
                    f"Skipped legacy sandbox pod {pod_name}: raw owner missing"
                )
                continue
            try:
                await self._ensure_pvc(
                    self._workspace_pvc_name(pod)
                    or f"workspace-{self._pod_resource_id(pod)}",
                    raw_owner,
                    loop,
                    allow_unowned_adoption=True,
                )
            except Exception as exc:
                # Do not put a pod with unverifiable/mismatched persistent
                # storage into the in-memory ownership registry.
                log.error(
                    f"Skipped sandbox pod {pod_name}: PVC ownership check "
                    f"failed ({type(exc).__name__})"
                )
                continue
            user_id = raw_owner
            phase = pod.status.phase if pod.status else "Unknown"
            status = ContainerStatus.RUNNING if phase == "Running" else ContainerStatus.STOPPED

            info = self._container_info_from_pod(
                pod,
                name=self._pod_display_name(pod) or pod_name,
                api_key=self._extract_api_key(pod),
                status=status,
                user_id=user_id,
            )

            self._store_container_state(info, user_id=user_id)

            # Ensure the corresponding Service exists so the host DNS resolves
            if status == ContainerStatus.RUNNING:
                try:
                    await self._ensure_service(
                        pod_name,
                        selector=self._service_selector_from_pod(pod),
                        loop=loop,
                    )
                except Exception as exc:
                    log.warning(
                        f"Failed to ensure service for pod {pod_name} during "
                        f"reconcile: {type(exc).__name__}"
                    )

        log.info(f"Reconciled {len(pods.items)} sandbox pods")

    async def cleanup_all(self) -> None:
        """GKE mode: only clear memory state, don't kill pods."""
        self._containers.clear()
        self._api_keys.clear()
        self._container_owners.clear()
        self._container_projects.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_existing_pod(
        self,
        user_id: str,
        loop: asyncio.AbstractEventLoop,
    ):
        try:
            pods = await loop.run_in_executor(
                None,
                lambda: self.core_api.list_namespaced_pod(
                    self.namespace,
                    label_selector="app=openbox-sandbox",
                ),
            )
        except ApiException:
            return None

        for pod in pods.items:
            if self._pod_user_id(pod) == user_id:
                return pod
        return None

    def _pod_annotations(self, pod) -> dict[str, str]:
        return pod.metadata.annotations or {}

    def _pod_labels(self, pod) -> dict[str, str]:
        return pod.metadata.labels or {}

    def _pod_user_id(self, pod) -> str:
        annotations = self._pod_annotations(pod)
        labels = self._pod_labels(pod)
        return annotations.get("openbox.dev/user-id-raw") or labels.get("openbox.dev/user-id", "")

    def _pod_display_name(self, pod) -> str:
        return self._pod_annotations(pod).get("openbox.dev/display-name", "")

    def _pod_resource_id(self, pod) -> str:
        labels = self._pod_labels(pod)
        return labels.get("openbox.dev/resource-id") or pod.metadata.name.removeprefix("sandbox-")

    def _service_selector_from_pod(self, pod) -> dict[str, str]:
        labels = self._pod_labels(pod)
        if labels.get("openbox.dev/resource-id"):
            return {
                "app": "openbox-sandbox",
                "openbox.dev/resource-id": labels["openbox.dev/resource-id"],
            }
        return {
            "app": "openbox-sandbox",
            "openbox.dev/user-id": labels.get("openbox.dev/user-id", ""),
        }

    def _workspace_pvc_name(self, pod) -> str | None:
        if not pod.spec:
            return None
        for volume in pod.spec.volumes or []:
            if volume.name == "workspace" and volume.persistent_volume_claim:
                return volume.persistent_volume_claim.claim_name
        return None

    async def _ensure_pvc(
        self,
        pvc_name: str,
        user_id: str,
        loop: asyncio.AbstractEventLoop,
        *,
        allow_unowned_adoption: bool = False,
    ):
        try:
            existing = await loop.run_in_executor(
                None, lambda: self.core_api.read_namespaced_persistent_volume_claim(pvc_name, self.namespace),
            )
            owner = (existing.metadata.annotations or {}).get("openbox.dev/user-id-raw")
            if owner and owner != user_id:
                raise PermissionError(f"PVC {pvc_name} belongs to a different sandbox owner")
            if not owner:
                if not allow_unowned_adoption:
                    raise PermissionError(
                        f"PVC {pvc_name} has no verifiable sandbox owner"
                    )
                await self._adopt_pvc_owner(pvc_name, user_id, loop)
            return
        except ApiException as e:
            if e.status != 404:
                raise

        pvc = k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(
                name=pvc_name,
                labels={
                    "app": "openbox-sandbox",
                    "openbox.dev/resource-id": _resource_id(user_id),
                },
                annotations={"openbox.dev/user-id-raw": user_id},
            ),
            spec=k8s_client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name=self.config.k8s_storage_class,
                resources=k8s_client.V1VolumeResourceRequirements(
                    requests={"storage": self.config.k8s_storage_size},
                ),
            ),
        )
        try:
            await loop.run_in_executor(
                None,
                lambda: self.core_api.create_namespaced_persistent_volume_claim(
                    self.namespace, pvc
                ),
            )
            log.info(f"PVC {pvc_name} created ({self.config.k8s_storage_size})")
        except ApiException as e:
            if e.status != 409:
                raise
            existing = await loop.run_in_executor(
                None,
                lambda: self.core_api.read_namespaced_persistent_volume_claim(
                    pvc_name, self.namespace
                ),
            )
            owner = (existing.metadata.annotations or {}).get("openbox.dev/user-id-raw")
            if owner != user_id:
                raise PermissionError(f"PVC {pvc_name} belongs to a different sandbox owner")
            log.info(f"PVC {pvc_name} already exists, reusing")

    async def _adopt_pvc_owner(
        self,
        pvc_name: str,
        user_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Attach durable ownership metadata to a pre-migration PVC."""
        await loop.run_in_executor(
            None,
            lambda: self.core_api.patch_namespaced_persistent_volume_claim(
                pvc_name,
                self.namespace,
                {
                    "metadata": {
                        "labels": {
                            "app": "openbox-sandbox",
                            "openbox.dev/resource-id": _resource_id(user_id),
                        },
                        "annotations": {"openbox.dev/user-id-raw": user_id},
                    }
                },
            ),
        )
        log.info(f"PVC {pvc_name} adopted into the sandbox ownership protocol")

    async def _adopt_pod_owner(
        self,
        pod_name: str,
        user_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Persist the raw owner on an unambiguous pre-migration Pod."""
        await loop.run_in_executor(
            None,
            lambda: self.core_api.patch_namespaced_pod(
                pod_name,
                self.namespace,
                {
                    "metadata": {
                        "annotations": {"openbox.dev/user-id-raw": user_id}
                    }
                },
            ),
        )
        log.info(f"Pod {pod_name} adopted into the sandbox ownership protocol")

    async def _ensure_service(self, svc_name: str, selector: dict, loop: asyncio.AbstractEventLoop):
        try:
            existing = await loop.run_in_executor(
                None, lambda: self.core_api.read_namespaced_service(svc_name, self.namespace),
            )
            self._assert_service_selector(svc_name, existing, selector)
            return
        except ApiException as e:
            if e.status != 404:
                raise

        svc = k8s_client.V1Service(
            metadata=k8s_client.V1ObjectMeta(name=svc_name),
            spec=k8s_client.V1ServiceSpec(
                type="ClusterIP",
                selector=selector,
                ports=[k8s_client.V1ServicePort(port=8000, target_port=8000)],
            ),
        )
        try:
            await loop.run_in_executor(
                None, lambda: self.core_api.create_namespaced_service(self.namespace, svc),
            )
            log.info(f"Service {svc_name} created")
        except ApiException as e:
            if e.status != 409:
                raise
            existing = await loop.run_in_executor(
                None,
                lambda: self.core_api.read_namespaced_service(svc_name, self.namespace),
            )
            self._assert_service_selector(svc_name, existing, selector)
            log.info(f"Service {svc_name} already exists, reusing")

    @staticmethod
    def _assert_service_selector(svc_name: str, service, expected: dict) -> None:
        actual = (service.spec.selector or {}) if service.spec else {}
        if any(actual.get(key) != value for key, value in expected.items()):
            raise PermissionError(
                f"Service {svc_name} selects a different sandbox resource"
            )

    async def _wait_pod_ready(self, pod_name: str, loop: asyncio.AbstractEventLoop):
        max_attempts = int(self.config.container_ready_timeout / self.config.container_ready_interval)
        for attempt in range(max_attempts):
            try:
                pod = await loop.run_in_executor(
                    None, lambda: self.core_api.read_namespaced_pod(pod_name, self.namespace),
                )
                if pod.status and pod.status.phase == "Running":
                    conditions = pod.status.conditions or []
                    for cond in conditions:
                        if cond.type == "Ready" and cond.status == "True":
                            log.info(f"Pod {pod_name} ready after {attempt + 1} attempts")
                            return
            except ApiException:
                pass
            await asyncio.sleep(self.config.container_ready_interval)
        raise TimeoutError(f"Pod {pod_name} did not become ready within {self.config.container_ready_timeout}s")

    async def _wait_pod_deleted(self, pod_name: str, loop: asyncio.AbstractEventLoop):
        max_attempts = int(self.config.container_ready_timeout / self.config.container_ready_interval)
        for _ in range(max_attempts):
            try:
                await loop.run_in_executor(
                    None, lambda: self.core_api.read_namespaced_pod(pod_name, self.namespace),
                )
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            await asyncio.sleep(self.config.container_ready_interval)
        raise TimeoutError(f"Pod {pod_name} was not deleted within {self.config.container_ready_timeout}s")

    async def _wait_action_server(self, host: str, port: int):
        url = f"http://{host}:{port}/alive"
        max_attempts = int(self.config.container_ready_timeout / self.config.container_ready_interval)
        async with httpx.AsyncClient() as client:
            for attempt in range(max_attempts):
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        log.info(f"Action server at {host}:{port} ready")
                        return
                except (httpx.ConnectError, httpx.ReadTimeout):
                    pass
                except Exception as exc:
                    log.debug(
                        f"Action server check attempt {attempt + 1}: "
                        f"{type(exc).__name__}"
                    )
                await asyncio.sleep(self.config.container_ready_interval)
        raise TimeoutError(f"Action server at {host}:{port} did not become ready")

    async def _trigger_backup(self, info: ContainerInfo):
        """Trigger workspace backup via the action server's /backup endpoint."""
        payload = self._build_backup_payload(info)
        if payload is None:
            return
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"http://{info.host}:{info.port}/backup",
                    headers={"X-API-Key": self._api_keys.get(info.id, "")},
                    json=payload,
                )
                if resp.status_code == 200:
                    log.info(f"Backup triggered for {info.id}: {resp.json()}")
                else:
                    log.warning(f"Backup failed for {info.id}: {resp.status_code}")
        except Exception as exc:
            log.warning(
                f"Backup request failed for {info.id}: {type(exc).__name__}"
            )

    async def _trigger_restore(self, info: ContainerInfo):
        """Trigger workspace restore via the action server's /restore endpoint."""
        payload = self._build_backup_payload(info)
        if payload is None:
            return
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"http://{info.host}:{info.port}/restore",
                    headers={"X-API-Key": self._api_keys.get(info.id, "")},
                    json=payload,
                )
                if resp.status_code == 200:
                    log.info(f"Restore triggered for {info.id}: {resp.json()}")
                else:
                    log.warning(f"Restore returned {resp.status_code} for {info.id}")
        except Exception as exc:
            log.warning(
                f"Restore request failed for {info.id}: {type(exc).__name__}"
            )

    def _extract_api_key(self, pod) -> str:
        if pod.spec and pod.spec.containers:
            for env in (pod.spec.containers[0].env or []):
                if env.name == "SESSION_API_KEY":
                    return env.value or ""
        return ""

    def _container_info_from_pod(
        self,
        pod,
        *,
        name: str,
        api_key: str,
        status: ContainerStatus | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> ContainerInfo:
        phase = pod.status.phase if pod.status else "Unknown"
        container_status = status or (ContainerStatus.RUNNING if phase == "Running" else ContainerStatus.STOPPED)
        pod_name = pod.metadata.name
        image = pod.spec.containers[0].image if pod.spec and pod.spec.containers else self.config.sandbox_image
        return ContainerInfo(
            id=pod_name,
            name=name,
            status=container_status,
            image=image,
            created_at=pod.metadata.creation_timestamp or datetime.now(timezone.utc),
            host=f"{pod_name}.{self.namespace}.svc.cluster.local",
            port=8000,
            api_key=api_key,
        )

    def _store_container_state(
        self,
        info: ContainerInfo,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._containers[info.id] = info
        self._api_keys[info.id] = info.api_key or ""
        if user_id:
            self._container_owners[info.id] = user_id
        if project_id:
            self._container_projects[info.id] = project_id

    def _build_backup_payload(self, info: ContainerInfo) -> dict | None:
        if getattr(self.config, "blob_provider", "") != "gcs":
            return None
        bucket = getattr(self.config, "gcs_bucket", "")
        if not bucket:
            return None
        user_id = self._container_owners.get(info.id)
        if not user_id:
            return None
        # This prefix is an ownership boundary, not a display label. Reuse the
        # canonical ULID/default path for existing tenants, and hash every
        # other raw id so normalization cannot merge two users' backups.
        prefix = f"users/{_resource_id(user_id)}/workspace/"
        return {
            "provider": "gcs",
            "bucket": bucket,
            "prefix": prefix,
        }
