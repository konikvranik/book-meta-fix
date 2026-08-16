[English](../../how-to/kubernetes.md) | **Čeština**

# Spuštění bmf v clusteru Kubernetes

Multiarch Docker obraz (viz `Dockerfile` a workflow `docker` na GitHub
Actions — `linux/amd64`, `linux/arm64`, `linux/arm/v7`) vám umožňuje
spouštět dávkové příkazy na libovolných uzlech clusteru, včetně workerů
Raspberry Pi / ARM. `analyze`, `organize` a `crosscheck` jsou dávkové
úlohy — každou spouštějte jako Kubernetes `Job`, ne jako dlouho běžící
Deployment.

Revizní smyčka je stále human-in-the-loop: `analyze` vytvoří `review.yaml`
**uvnitř volume s knihovnou**; ten upravíte (lokálně, nebo pomocí
`bmf gui`) a pak spustíte `bmf apply` — opět Job proti témuž volume.
Každý měnící příkaz je ve výchozím nastavení dry-run, takže Job bez
`--apply` v argumentech je vždy bezpečný.

## Předpoklady

- Knihovna na sdíleném úložišti, ke kterému mají pracovní uzly přístup pro
  čtení i zápis — `analyze` zapisuje `review.yaml`, `bmf_cache.db`
  a razí uuid do `metadata.json`. Pro homelab cluster je obvyklou volbou
  NFS (viz níže).
- Obraz dostupný pro cluster — GHCR (nahrává CI) nebo lokální registry.
  Upravte referenci na obraz v Jobech níže.
- API klíče (`ZAI_API_KEY`) jako Secret, nikdy přímo v manifestu.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bmf-env
type: Opaque
stringData:
  ZAI_API_KEY: "sk-..."
```

Obraz už nastavuje `BMF_LIBRARY=/library`, stačí tedy připojit úložiště
na `/library`.

## Připojení knihovny z NFS

Buď předem zřízený NFS `PersistentVolume` + claim, nebo (jednodušší pro
homelab) inline volume `nfs` v každém Jobu:

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

Pokud nechcete objekty PV, přeskočte je a použijte v Jobu místo toho
inline formu (vyžaduje na uzlech NFS CSI driver / mounter):

```yaml
      volumes:
        - name: library
          nfs:
            server: nfs.example.com
            path: /export/books
            readOnly: false
```

## Samostatné mounty pro review.yaml a cache databázi

Ve výchozím nastavení skončí oba soubory v kořeni knihovny (`review.yaml`
vedle knih, `bmf_cache.db` v pracovním adresáři). Držet je na *vlastním*
volume předávání usnadní — `review.yaml` můžete upravovat z připojení
téhož share na vaší pracovní stanici, aniž byste procházeli 5 000 složek
knih — a dovolí cache žít na rychlejším lokálním úložišti:

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

Poznámky:

- `analyze --output` rozhoduje, kam se revizní soubor zapíše; `BMF_REVIEW`
  je záložní výchozí hodnota pro `apply`/`gui`, takže oba ukazují na týž
  soubor. `apply` pak běží bez pozičního argumentu:
  `args: ["apply", "--apply"]`.
- Přenos `.bak` (`review.yaml.bak`) se zapisuje vedle revizního souboru —
  na témtéž volume, takže předchozí rozhodnutí přežijí napříč Joby.
- `emptyDir` pro cache je v pořádku (je to cache — cold rebuild stojí jen
  čas, na NFS se tím navíc vyhnete problémům se zamykáním
  SQLite-over-NFS). Chcete-li, aby po sobě jdoucí Joby cache znovu
  používaly, použijte místo toho `hostPath`/PVC; pak ale držte Joby
  přísně sekvenčně.

## Jednořádkovky: `scripts/k8s-bmf.sh`

Job manifesty výše jsou explicitní verze. Pro každodenní použití tu je
wrapper — `scripts/k8s-bmf.sh` — který přes `kubectl run --rm -i`
spustí one-shot pod, streamuje výstup, **po dokončení příkazu pod smaže**
a propaguje jeho exit kód. Celá revizní smyčka:

```bash
# 1. analyze: detectors + enrichment + LLM -> /review/review.yaml on the PVC
./scripts/k8s-bmf.sh analyze --llm

# 2. edit review.yaml (your workstation mount of the same share, or bmf gui)

# 3. apply the approved fixes
./scripts/k8s-bmf.sh apply --apply

# 4. reorganize the library
./scripts/k8s-bmf.sh organize --apply

# 5. quarantine rogue format files
./scripts/k8s-bmf.sh crosscheck --apply
```

Další přepínače se předají rovnou `bmf` (`./scripts/k8s-bmf.sh
organize --pattern "{author_sort}/{title} ({id})" --apply`). Wrapper čte
své výchozí hodnoty z prostředí — obraz, NFS server/cestu
(`BMF_K8S_NFS_SRV=""` přepne na PVC `books`), název secretu a umístění
`BMF_REVIEW`/`BMF_CACHE` — viz hlavičku skriptu. Očekává PVC `bmf-review`
z níže uvedené sekce; cache žije na `emptyDir`, takže mimo `review.yaml`
(+ `.bak`) nic nepersistuje.

## analyze (vygeneruje review.yaml)

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

Spusťte a sledujte výstup:

```bash
kubectl apply -f bmf-analyze.yaml
kubectl logs -f job/bmf-analyze
```

Po dokončení Jobu leží `review.yaml` vedle knih na PVC (nebo na volume
`/review`, pokud jste mounty rozdělili, jak je uvedeno výše). Upravte jej
(viz [edit-and-apply.md](edit-and-apply.md)) a spusťte Job `apply`
s `args: ["apply", "/review/review.yaml", "--apply"]` — nebo prostě
`args: ["apply", "--apply"]` s nastaveným `BMF_REVIEW`.

## organize (restrukturalizace knihovny)

Stejná kostra Jobu, jiné `name` a `args`. První běh nechte bez `--apply`,
abyste si v logu přečetli dry-run plán:

```yaml
metadata:
  name: bmf-organize
spec:
  template:
    spec:
      containers:
        - name: bmf
          image: ghcr.io/konikvranik/book-meta-fix:latest
          args: ["organize", "--library", "/library"]          # dry-run
          # args: ["organize", "--library", "/library", "--apply"]
```

Node selector pro clustery pouze s ARM (obraz je multiarch, ale možná
chcete připnout uzel s rychlým diskem):

```yaml
      nodeSelector:
        kubernetes.io/arch: arm64
```

## crosscheck (karanténa zbloudilých souborů formátů)

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

## Zádrhele

- **Umístění SQLite cache**: `bmf_cache.db` má výchozí umístění v
  *aktuálním pracovním adresáři* (`/library` v obrazu). Přesměrujte jej
  proměnnou `BMF_CACHE` — viz
  [Samostatné mounty](#samostatné-mounty-pro-reviewyaml-a-cache-databázi);
  držte jej mimo NFS (zamykání) a nikdy nemiřte dva souběžně běžící Joby
  na týž soubor cache — Joby spouštějte sekvenčně
  (např. `kubectl wait --for=condition=complete`).
- **Jeden Job najednou**: `analyze` / `apply` / `organize` všechny
  zapisují do knihovny; serializujte je. Jednoduchý řetěz `CronJob`ů nebo
  Argo/Airflow DAG funguje, chcete-li naplánované běhy.
- **Obálky a externí nástroje**: obraz obsahuje poppler (`pdftotext` /
  `pdfinfo`), ale ne calibre ani tesseract — `epubgen` z ne-EPUB zdrojů
  a OCR skenovaných PDF jsou v clusteru nedostupné.
- **GUI v k8s neběží**: `bmf gui` je desktopová aplikace Tkinter. Cluster
  spouští dávkové příkazy; revizní soubor na sdíleném volume je místem
  předání.
- **Prostředky**: `analyze` s `--llm` je vázané na síť; extrakce obsahu
  (text PDF/EPUB) je vázaná na CPU. `requests: {cpu: "1", memory: 1Gi}`
  je rozumný začátek pro knihovnu s ~5k knihami.
