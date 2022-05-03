from kubernetes import client, config
from kubernetes.client.rest import ApiException
from datetime import datetime
from alive_progress import alive_bar # https://github.com/rsalmei/alive-progress/issues/159
import time
import argparse
import asyncio
import json_logging, logging, sys
import pytz

async def main():
  logger.debug(f'Config: {args}')
  
  start = time.time()

  config.load_kube_config()

  timeStamp = datetime.now().isoformat(timespec='seconds')
  logger.debug(f'Using {timeStamp} as timestamp for annotation')

  if args.rollout_reason is not None:
    rollout_reason = args.rollout_reason
  else:
    rollout_reason = f'Update sidecars {" and ".join(args.sidecar_container_name)}'

  # Patch used to patch resources to trigger a rolling update
  patch = {
    "spec": {
      "template": {
        "metadata": {
          "annotations": {
            args.annotation_prefix + ".rollout.timestamp": timeStamp,
            args.annotation_prefix + ".rollout.reason": rollout_reason
          }
        }
      }
    }
  }

  logger.debug(f'Using patch {patch}')

  updateQueue = asyncio.Queue()
  resultQueue = asyncio.Queue()

  # Find all workloads that need to be updated, add them to a de-duplicated dict/list
  workloads_to_update, _, errors = get_workloads_to_update()
  await queue_updates(workloads_to_update, patch, updateQueue)

  # Create X number of workers that are ready to receive instructions on a workload to update and wait for
  [asyncio.create_task(updateWorkload(n, updateQueue, resultQueue)) for n in range(int(args.parallel_rollouts))]

  await monitor_queue(start, updateQueue)

  # Wait for all update tasks to complete
  await updateQueue.join()

  # Retrieve and process results
  errors += await process_results(resultQueue)

  logger.info(f'Completed after {round(time.time() - start, 2)}s with {errors} errors')

  # Probably not optimal. But couldn't find a good way to make asyncio.run deal with exit codes.
  if errors > 0:
    sys.exit(1)

  sys.exit(0)

async def monitor_queue(start, q: asyncio.Queue):
  initialQueueSize = q.qsize()
  previousQueueSize = initialQueueSize
  with alive_bar(initialQueueSize, title='Rollout progress', stats=False) as bar:
    while True:
      if q.qsize() == 0:
        return
      await asyncio.sleep(1)

      queueChange = previousQueueSize - q.qsize()
      for i in range(queueChange):
        #logger.info(f'PROGRESS: {initialQueueSize - q.qsize()} of {initialQueueSize} rollouts complete after {time.time() - start}s')
        bar()
      previousQueueSize = q.qsize()

# Process results from result queue and tally up errors
async def process_results(r: asyncio.Queue):
  errors = 0

  while True:
    if r.qsize() == 0:
      return errors

    error, time, message = await r.get()
    if error:
      errors += 1
      logger.error(f'{time.isoformat(timespec="seconds")}: {message}')
    else:
      logger.debug(f'{time.isoformat(timespec="seconds")}: {message}')

# Get pods, determine which to update, get the owner and put into update list
async def queue_updates(workloads_to_update, patch, q: asyncio.Queue):
  for kind in workloads_to_update:
    for namespace in workloads_to_update[kind] :
      for name in workloads_to_update[kind][namespace]:
        await q.put((kind, namespace, name, patch))

# Get pods, determine which to update, get the owner and put into update list
def get_workloads_to_update():

  workloads_to_update = {
    "Deployment": {},
    "StatefulSet": {},
    "DaemonSet": {}
  }

  errors = 0
  number_of_updates = 0

  utc=pytz.UTC
  pod_start_time_cutoff = datetime.now()
  if args.only_started_before is not None:
    pod_start_time_cutoff = datetime.fromisoformat(args.only_started_before)
  pod_start_time_cutoff = utc.localize(pod_start_time_cutoff) 

  # Get and cache all replicasets and index by namespace and name
  # so that we can quickly get the Deployment that owns a Pod
  replicasets = {}
  appsv1 = client.AppsV1Api()
  ret = appsv1.list_replica_set_for_all_namespaces(watch=False)
  for i in ret.items:
    if i.metadata.namespace not in replicasets:
      replicasets[i.metadata.namespace] = {}
    replicasets[i.metadata.namespace][i.metadata.name] = i

  corev1 = client.CoreV1Api()
  ret = corev1.list_pod_for_all_namespaces(watch=False)
  for i in ret.items:

    if args.include_namespace is not None:
      if i.metadata.namespace not in args.include_namespace:
        logger.debug(f'Pod {i.metadata.namespace}/{i.metadata.name} is not in list of included namespaces. Skipping.')
        continue

    if args.exclude_namespace is not None:
      if i.metadata.namespace in args.exclude_namespace:
        logger.debug(f'Pod {i.metadata.namespace}/{i.metadata.name} is in list of excluded namespaces. Skipping.')
        continue

    if not hasSideCarWithName(i.spec.containers, args.sidecar_container_name):
      logger.debug(f'Pod {i.metadata.name} does NOT have any of {args.sidecar_container_name} container. Skipping.')
      continue

    if i.status.start_time < pod_start_time_cutoff:
      logger.debug(f'Pod {i.metadata.namespace}/{i.metadata.name} started at {i.status.start_time} which is after {pod_start_time_cutoff}. Skipping.')
      continue

    hasOwner, error, ownerKind, ownerName = getOwner(i)
    if error != "":
      logger.error(f'{i.metadata.namespace}/{i.metadata.name} error getting pod owner: {error}')
      errors += 1
      continue

    # This is special for ReplicaSets and will replace with name and kind of Deployment that owns the ReplicaSet
    if ownerKind == "ReplicaSet":
      hasOwner, error, ownerKind, ownerName = getOwner(replicasets[i.metadata.namespace][ownerName])
      if error != "":
        logger.error(f'{i.metadata.namespace}/{i.metadata.name} error getting ReplicaSet owner: {error}')
        errors += 1
        continue

    if ownerKind not in ["Deployment", "DaemonSet", "StatefulSet"]:
      logger.error(f'{i.metadata.namespace}/{i.metadata.name} unknown kind {ownerKind}')
      errors += 1
      continue

    if ownerKind == "Deployment" and args.exclude_deployment:
      logger.debug(f'{i.metadata.namespace}/{i.metadata.name} is owner by Deployment but exclude_deployment is enabled. Skipping.')
      continue
    
    if ownerKind == "DaemonSet" and not args.include_daemonset:
      logger.debug(f'{i.metadata.namespace}/{i.metadata.name} is owner by DaemonSet but include_daemonset is not enabled. Skipping.')
      continue

    if ownerKind == "StatefulSet" and not args.include_statefulset:
      logger.debug(f'{i.metadata.namespace}/{i.metadata.name} is owner by StatefulSet but include_statefulset is not enabled. Skipping.')
      continue

    logger.info(f'Pod {i.metadata.namespace}/{i.metadata.name} has one of {args.sidecar_container_name} sidecars and is owned by {ownerKind}/{ownerName}. Adding to update list.')

    if i.metadata.namespace not in workloads_to_update[ownerKind]:
      workloads_to_update[ownerKind][i.metadata.namespace] = {}

    workloads_to_update[ownerKind][i.metadata.namespace][ownerName] = ownerName
    number_of_updates += 1

  return workloads_to_update, number_of_updates, errors


async def updateWorkload(id: int, q: asyncio.Queue, r: asyncio.Queue) -> None:
  logger.debug(f'worker {id}: starting')
  while True:
    kind, namespace, name, patch = await q.get()
    start = time.time()
    logger.debug(f'worker {id}: {kind}/{namespace}/{name} being updated')

    appsv1 = client.AppsV1Api()

    if not args.confirm:
      await r.put((False, datetime.now(), f'worker {id}: {kind}/{namespace}/{name} not updated since --confirm is not set'))
      q.task_done()
      continue

    error = ""
    # kubectl rollout status will fail with `error: timed out waiting for the condition` if it times out.
    try:
      if kind == "Deployment":
        api_response = appsv1.patch_namespaced_deployment(name, namespace,  patch,  pretty=True) # , dry_run="All", field_manager=field_manager
        process = await asyncio.create_subprocess_shell(f'kubectl rollout status deployment {name} -n {namespace} --timeout={args.timeout}')
      elif kind == "StatefulSet":
        api_response = appsv1.patch_namespaced_stateful_set(name, namespace,  patch,  pretty=True) # , dry_run="All", field_manager=field_manager
        process = await asyncio.create_subprocess_shell(f'kubectl rollout status statefulset {name} -n {namespace} --timeout={args.timeout}')
      elif kind == "DaemonSet":
        api_response = appsv1.patch_namespaced_daemon_set(name, namespace,  patch,  pretty=True) # , dry_run="All", field_manager=field_manager
        process = await asyncio.create_subprocess_shell(f'kubectl rollout status daemonset {name} -n {namespace} --timeout={args.timeout}')
      else:
        error = f'Unknown Kind {kind}'

      stdout, stderr = await process.communicate()

      # Possible bug here where errors from kubectl isn't actually getting caught
      if process.returncode != 0: 
        error = f'Rollout failed: {process.stderr}'

    except ApiException as e:
        error = f'Exception when calling patch: {e}'

    if error != "":
      await r.put((True, datetime.now(), f'worker {id}: {kind}/{namespace}/{name} update failed: {error}'))
    else:
      await r.put((False, datetime.now(), f'worker {id}: {kind}/{namespace}/{name} updated successfully in {round(time.time() - start, 2)}s'))

    logger.info(f'worker {id}: {kind}/{namespace}/{name} update done in {round(time.time() - start, 2)}s')
    q.task_done()

# Returns: hasOwner bool, err string, ownerKind string, ownerName string
def getOwner(i):
  # Does not have owner. Root object?
  if len(i.metadata.owner_references) == 0:
    return False, "", "", ""
  
  if len(i.metadata.owner_references) > 1:
    return True, f'{i.metadata.name} has multiple owners. We don\'t support multiple owners.', "", ""

  return True, "", i.metadata.owner_references[0].kind, i.metadata.owner_references[0].name

# Takes a list of containers and container names to search for.
# Returns true if any of the container names exists in any of the containers
def hasSideCarWithName(containers, containerNames):
  for c in containers:
    for cName in containerNames:
      if c.name == cName:
        return True
  return False

# If called directly, parse arguments and configure logging
if __name__ == "__main__":
  parser = argparse.ArgumentParser(description='Add/update annotation on Deployment/ReplicaSet/DaemonSet for Pods with injected sidecars.')
  parser.add_argument('--sidecar-container-name', required=True, action='append', help='Name of sidecar container to search for. Can be specified multiple times.')
  parser.add_argument('--exclude-deployment', type=bool, default=False, help='Exclude rollout of Deployments')
  parser.add_argument('--include-daemonset', type=bool, default=False, help='Include rollout of DaemonSets')
  parser.add_argument('--include-statefulset', type=bool, default=False, help='Include rollout of StatefulSets')
  parser.add_argument('--include-namespace', action='append', help='Only process this namespace. Can be specified multiple times.')
  parser.add_argument('--exclude-namespace', action='append', help='Exclude namespace. Can be specified multiple times. Cannot be used with --include-namespace.')
  parser.add_argument('--annotation-prefix', default='sidecarRollout', help='Prefix for annotations.')
  parser.add_argument('--rollout-reason', help='Text inserted in rollout.reason annotation.')
  parser.add_argument('--timeout', default="300s", help='Timeout for waiting for rollout to become ready.')
  parser.add_argument('--confirm', type=bool, default=False, help='Actually do rollout. Without this we only do a dry-run showing what would be done.')
  parser.add_argument('--log-format', default="text", help='Log format. "json" or "text"')
  parser.add_argument('--log-level', default="info", help='Log level. debug, info, warning, error, critical')
  parser.add_argument('--parallel-rollouts', default=1, help='Number of rollouts to run in parallel.')
  parser.add_argument('--only-started-before', help='Only rollout workload if it contains at least one Pod started before this time, (in UTC). Example: 2022-05-01 13:00') # https://docs.python.org/3/library/datetime.html#datetime.datetime.fromisoformat

  args = parser.parse_args()

  if args.include_namespace is not None and args.exclude_namespace is not None:
    sys.exit('--include-namespace and --exclude-namespace cannot be used in combination')

  log_level = getattr(logging, args.log_level.upper(), None)
  # If you also want to have debug logs from kubernetes-client add `level=log_level` to basicConfig.
  logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s')

  logger = logging.getLogger('sidecarrollout')
  logger.setLevel(level=log_level)

  if args.log_format == "json":
    json_logging.init_non_web(enable_json=True)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.propagate = False

  asyncio.run(main())
