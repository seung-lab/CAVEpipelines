env:
  - name: &commonEnvVars "pychunkedgraph"
    vars:
      REDIS_HOST: "${redis_host}"
      REDIS_PORT: 6379
      REDIS_PASSWORD: ""
      BIGTABLE_PROJECT: &bt_project "${google_project}"
      BIGTABLE_INSTANCE: &bt_instance "${bigtable_instance}"
      GOOGLE_APPLICATION_CREDENTIALS: /root/.cloudvolume/secrets/google-secret.json
      SHELL: /bin/bash
      FLASK_APP: run_dev.py
      APP_SETTINGS: pychunkedgraph.app.config.DeploymentWithRedisConfig

configfiles:
- name: &bashrc "bashrc"
  files:
    ".bashrc": |-
      alias watch='watch '
      alias ingest='flask ingest'
      alias rqx='flask rq'

configyamls:
- name: &dataset test
  files:
  - name: test.yml
    content:
      data_source:
        EDGES: "<path_to_edges>"
        COMPONENTS: "<path_to_components>"
        WATERSHED: "<path_to_segmentation>"

      graph_config:
        CHUNK_SIZE: [] # [X, Y, Z]
        FANOUT: 2
        SPATIAL_BITS: 2
        LAYER_ID_BITS: 8

      backend_client:
        TYPE: "bigtable"
        CONFIG:
          ADMIN: true
          READ_ONLY: false
          PROJECT: *bt_project
          INSTANCE: *bt_instance

secrets:
- name: &cloudVolumeSecrets cloud-volume-secrets
  files:
    # these are used by python bigtable client and cloud-files
    # must have the following permissions:
    # * read gcs objects if edges/component files are stored in google cloud buckets
    #   if they're stored elsewhere use the secrets with appropriate permissions accordingly
    # * bigtable - create and read tables
    google-secret.json: |-
      {
        <contents_of_service_accout_secret>
      }

deployments:
- enabled: true
  name: &name master
  nodeSelector:
    cloud.google.com/gke-nodepool: master
  hpa:
    enabled: false
  volumes: &commonVolumes
  - name: *cloudVolumeSecrets
    secret:
      secretName: *cloudVolumeSecrets
  - name: &datasetsVolume datasets-volume
    configMap:
      name: *dataset
  - name: &bashrcVolume bashrc-volume
    configMap:
      name: *bashrc
  containers:
  - name: *name
    image: &image
      repository: <image_repo>
      tag: "<image_tag>"
    volumeMounts: &commonVolumeMounts
    - name: *cloudVolumeSecrets
      mountPath: /root/.cloudvolume/secrets
      readOnly: true
    - name: *datasetsVolume
      mountPath: /app/datasets
    - name: *bashrcVolume
      mountPath: /root/
    envFromConfigMap:
    - *commonEnvVars
    env:
    - name: MY_POD_IP
      valueFrom:
        fieldRef:
          fieldPath: status.podIP
    resources:
      requests:
        memory: 500M

workerDeployments:
- enabled: true
  name: &name l2
  nodeSelector:
    cloud.google.com/gke-nodepool: low
  hpa:
    enabled: false
  volumes: *commonVolumes
  containers:
  - name: *name
    command: [rq, worker, *name]
    image: *image
    volumeMounts: *commonVolumeMounts
    envFromConfigMap:
    - *commonEnvVars
    resources:
      requests:
        memory: 1G

- enabled: true
  name: &name l3
  nodeSelector:
    cloud.google.com/gke-nodepool: low
  hpa:
    enabled: false
  volumes: *commonVolumes
  containers:
  - name: *name
    command: [rq, worker, *name]
    image: *image
    volumeMounts: *commonVolumeMounts
    envFromConfigMap:
    - *commonEnvVars
    resources:
      requests:
        memory: 1.5G

- enabled: true
  name: &name l4
  nodeSelector:
    cloud.google.com/gke-nodepool: low
  hpa:
    enabled: false
  volumes: *commonVolumes
  containers:
  - name: *name
    command: [rq, worker, *name]
    image: *image
    volumeMounts: *commonVolumeMounts
    envFromConfigMap:
    - *commonEnvVars
    resources:
      requests:
        memory: 2G

trackerDeployments:
  count: 4 # number of layers in the chunkedgraph
  enabled: false
  volumes: *commonVolumes
  hpa:
    enabled: false
  containers:
  - image: *image
    volumeMounts: *commonVolumeMounts
    env:
    - name: *commonEnvVars
    resources:
      requests:
        memory: 100M