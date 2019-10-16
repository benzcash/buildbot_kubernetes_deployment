from buildbot.plugins import worker

"""   
- name: memory-demo-ctr
    image: polinux/stress
    resources:
      limits:
        memory: "200Mi"
      requests:
        memory: "100Mi" """

class ZcashBaseKubeLatentWorker(worker.KubeLatentWorker):
    def getBuildContainerResources(self, build):
        resources = {
                "limits": {
                    "memory": "30Gi",
                },
                "requests": {
                    "memory": "20Gi",
                }}
        return resources