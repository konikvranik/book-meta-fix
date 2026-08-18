# Running bmf in a Kubernetes cluster

**English** | [Čeština](../cs/how-to/kubernetes.md)

The multi-arch Docker image (see the `Dockerfile` and the
`docker` GitHub Actions workflow — `linux/amd64`, `linux/arm64`,
`linux/arm/v7`) lets you run the batch commands on whatever cluster nodes
you have, including Raspberry Pi / ARM workers. `analyze` and
`crosscheck` are batch jobs — run each as a Kubernetes `Job`, not a
long-lived Deployment.

The review loop is still human-in-the-loop: `analyze` produces
`review.yaml` **inside the library volume**; you edit it (locally, or with
`bmf gui`), then run `bmf apply` — again a Job against the same volume.
Every mutating command is dry-run by default, so a Job without `--apply`
in its args is always safe.

## Prerequisites

- The library on shared storage the worker nodes can access read-write —
  `analyze` writes `review.yaml`, `bmf_cache.db` and stamps uuids into
  `metadata.json`. NFS is the usual choice for a homelab cluster (see below).
- The image available to the cluster — GHCR (pushed by CI) or a local
  registry. Adjust the image reference in the Jobs below.
- API keys (`ZAI_API_KEY`) as a Secret, never in the manifest.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bmf-env
type: Opaque
stringData:
  ZAI_API_KEY: "sk-..."
```

The image already sets `BMF_LIBRARY=/library`, so only mount the storage at
`/library`.

## Mounting the library from NFS

Either a pre-provisioned NFS `PersistentVolume` + claim, or (simpler for a
homelab) an inline `nfs` volume in each Job:

```yaml
# Static provisioning — the share holds the whole library.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: books-nfs
spec:
  capacity:
    storage: 1Ti          # informational for NFS; not enforced
  accessModes: ["ReadWriteMany"]
  storageClassName: nfs
  nfs:
    server: nfs.example.com
    path: /export/books
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: books
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: nfs
  volumeName: books-nfs
  resources:
    requests:
      storage: 1Ti
```

If you don't want PV objects, skip them and use the inline form in the Job
instead (requires the NFS CSI driver / mounter on the nodes):

```yaml
      volumes:
        - name: library
          nfs:
            server: nfs.example.com
            path: /export/books
            readOnly: false
```

## Separate mounts for review.yaml and the cache DB

By default both land in the library root (`review.yaml` next to the books,
`bmf_cache.db` in the working directory). Keeping them on their *own* volume
makes the hand-off easier — you can edit `review.yaml` from your workstation
mount of the same share without browsing 5,000 book folders — and lets the
cache live on faster local storage:

```yaml
# In every bmf Job spec.template.spec:
      containers:
        - name: bmf
          image: ghcr.io/konikvranik/book-meta-fix:latest
          args: ["analyze", "--library", "/library", "--output", "/review/review.yaml", "--llm"]
          env:
            - name: BMF_REVIEW          # default for `apply` and `gui`
              value: /review/review.yaml
            - name: BMF_CACHE           # SQLite cache — keep OFF NFS if you can
              value: /cache/bmf_cache.db
          envFrom:
            - secretRef:
                name: bmf-env
          volumeMounts:
            - name: library
              mountPath: /library
            - name: review
              mountPath: /review        # holds review.yaml + review.yaml.bak
            - name: cache
              mountPath: /cache         # bmf_cache.db (emptyDir or a PVC)
      volumes:
        - name: library
          persistentVolumeClaim:
            claimName: books
        - name: review
          persistentVolumeClaim:
            claimName: bmf-review       # small RWX/RWO claim, or another NFS share
        - name: cache
          emptyDir: {}                  # ephemeral: cache rebuilds, correctness is unaffected
```

Notes:

- `analyze --output` decides where the review file is written;
  `BMF_REVIEW` is the fallback default for `apply`/`gui`, so both point at
  the same file. `apply` then runs without a positional argument:
  `args: ["apply", "--apply"]`.
- The `.bak` carry-over (`review.yaml.bak`) is written next to the review
  file — same volume, so prior decisions survive across Jobs.
- `emptyDir` for the cache is fine (it is a cache — a cold rebuild only
  costs time, on NFS it also dodges SQLite-over-NFS locking issues). Use a
  `hostPath`/PVC instead if you want consecutive Jobs to reuse it; then keep
  the Jobs strictly sequential.

## One-liners: `scripts/k8s-bmf.sh`

The Job manifests above are the explicit version. For day-to-day use there
is a wrapper — `scripts/k8s-bmf.sh` — that spawns a one-shot pod via
`kubectl run --rm -i`, streams the output, **deletes the pod when the
command finishes** and propagates its exit code. The whole review loop:

```bash
# 1. analyze: detectors + enrichment + LLM -> /review/review.yaml on the PVC
./scripts/k8s-bmf.sh analyze --llm

# 2. edit review.yaml (your workstation mount of the same share, or bmf gui)

# 3. apply the approved fixes
./scripts/k8s-bmf.sh apply --apply

# 4. (placement runs inside apply — every applied book is placed;
#    a standalone `organize` no longer exists)

# 5. quarantine rogue format files
./scripts/k8s-bmf.sh crosscheck --apply
```

Any extra flags pass straight through to `bmf` (`./scripts/k8s-bmf.sh
apply --apply --pattern "{author_sort}/{title} ({id})"`). The wrapper
reads its defaults from the environment — image, NFS server/path
(`BMF_K8S_NFS_SRV=""` switches to the `books` PVC), secret name, and the
`BMF_REVIEW`/`BMF_CACHE` locations — see the header of the script. It
expects the `bmf-review` PVC from the section below; the cache lives on an
`emptyDir`, so nothing persists except `review.yaml` (+ `.bak`).

## analyze (generate review.yaml)

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: bmf-analyze
spec:
  backoffLimit: 0            # a failed analyze should be inspected, not retried blindly
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: bmf
          image: ghcr.io/konikvranik/book-meta-fix:latest   # multi-arch; node arch is irrelevant
          args: ["analyze", "--library", "/library", "--llm"]
          envFrom:
            - secretRef:
                name: bmf-env
          volumeMounts:
            - name: library
              mountPath: /library
      volumes:
        - name: library
          persistentVolumeClaim:
            claimName: books
```

Run and follow the output:

```bash
kubectl apply -f bmf-analyze.yaml
kubectl logs -f job/bmf-analyze
```

When the Job completes, `review.yaml` sits next to the books on the PVC
(or on the `/review` volume if you split the mounts as shown above). Edit it
(see [edit-and-apply.md](edit-and-apply.md)) and run an `apply` Job with
`args: ["apply", "/review/review.yaml", "--apply"]` — or just
`args: ["apply", "--apply"]` with `BMF_REVIEW` set.

## apply (write metadata + place books)

Same Job skeleton, different `name` and `args`. Keep `--apply` off for the
first run to read the dry-run plan (planned writes and moves) in the logs.
The former `organize` Job is gone — placement is part of apply:

```yaml
metadata:
  name: bmf-apply
spec:
  template:
    spec:
      containers:
        - name: bmf
          image: ghcr.io/konikvranik/book-meta-fix:latest
          args: ["apply", "--library", "/library"]          # dry-run
          # args: ["apply", "--library", "/library", "--apply"]
```

Node selector for ARM-only clusters (the image is multi-arch, but you may
want to pin the node with the fast disk):

```yaml
      nodeSelector:
        kubernetes.io/arch: arm64
```

## crosscheck (quarantine rogue format files)

```yaml
metadata:
  name: bmf-crosscheck
spec:
  template:
    spec:
      containers:
        - name: bmf
          image: ghcr.io/konikvranik/book-meta-fix:latest
          args: ["crosscheck", "--library", "/library"]         # dry-run
          # args: ["crosscheck", "--library", "/library", "--apply"]
```

## Gotchas

- **SQLite cache location**: `bmf_cache.db` defaults to the *current working
  directory* (`/library` in the image). Redirect it with `BMF_CACHE` — see
  [Separate mounts](#separate-mounts-for-reviewyaml-and-the-cache-db); keep
  it off NFS (locking) and never point two concurrently running Jobs at the
  same cache file — run the Jobs sequentially
  (e.g. `kubectl wait --for=condition=complete`).
- **One Job at a time**: `analyze` / `apply` both write to the
  library; serialize them. A simple `CronJob` chain or an Argo/Airflow DAG
  works if you want scheduled runs.
- **Covers and external tools**: the image ships poppler (`pdftotext` /
  `pdfinfo`) but not calibre or tesseract — `epubgen` from non-EPUB sources
  and OCR of scanned PDFs are unavailable in-cluster.
- **The GUI does not run in k8s**: `bmf gui` is a Tkinter desktop app.
  The cluster runs the batch commands; the review file on the shared volume
  is the hand-off point.
- **Resources**: `analyze` with `--llm` is network-bound; content
  extraction (PDF/EPUB text) is CPU-bound. `requests: {cpu: "1", memory: 1Gi}`
  is a reasonable start for a ~5k-book library.
