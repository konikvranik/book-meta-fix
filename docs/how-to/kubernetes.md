# Running bmf in a Kubernetes cluster

The multi-arch Docker image (see the `Dockerfile` and the
`docker` GitHub Actions workflow — `linux/amd64`, `linux/arm64`,
`linux/arm/v7`) lets you run the batch commands on whatever cluster nodes
you have, including Raspberry Pi / ARM workers. `analyze`, `organize` and
`crosscheck` are batch jobs — run each as a Kubernetes `Job`, not a
long-lived Deployment.

The review loop is still human-in-the-loop: `analyze` produces
`review.yaml` **inside the library volume**; you edit it (locally, or with
`bmf gui`), then run `bmf apply` — again a Job against the same volume.
Every mutating command is dry-run by default, so a Job without `--apply`
in its args is always safe.

## Prerequisites

- The library on a `PersistentVolumeClaim` (or an NFS/hostPath mount the
  worker nodes can access read-write — `analyze` writes `review.yaml`,
  `bmf_cache.db` and stamps uuids into `metadata.json`).
- The image available to the cluster — GHCR (pushed by CI) or a local
  registry. Adjust `IMAGE` below.
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

The image already sets `BMF_LIBRARY=/library`, so only mount the claim at
`/library`.

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
          image: ghcr.io/pvranik/book-meta-fix:latest   # multi-arch; node arch is irrelevant
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

When the Job completes, `review.yaml` sits next to the books on the PVC.
Edit it (see [edit-and-apply.md](edit-and-apply.md)) and run an `apply` Job
with `args: ["apply", "/library/review.yaml", "--apply"]`.

## organize (restructure the library)

Same Job skeleton, different `name` and `args`. Keep `--apply` off for the
first run to read the dry-run plan in the logs:

```yaml
metadata:
  name: bmf-organize
spec:
  template:
    spec:
      containers:
        - name: bmf
          image: ghcr.io/pvranik/book-meta-fix:latest
          args: ["organize", "--library", "/library"]          # dry-run
          # args: ["organize", "--library", "/library", "--apply"]
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
          image: ghcr.io/pvranik/book-meta-fix:latest
          args: ["crosscheck", "--library", "/library"]         # dry-run
          # args: ["crosscheck", "--library", "/library", "--apply"]
```

## Gotchas

- **SQLite cache on the PVC**: `bmf_cache.db` is written into the *current
  working directory*, which the image sets to `/library`. Keep it on the
  PVC so consecutive Jobs reuse it (or set `BMF_CACHE=/library/bmf_cache.db`
  explicitly). Never point two concurrently running Jobs at the same cache
  file — run the Jobs sequentially (e.g. `kubectl wait --for=condition=complete`).
- **One Job at a time**: `analyze` / `apply` / `organize` all write to the
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
