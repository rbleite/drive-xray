# drive-xray

Indexa drives (Mac interno + externos) num "raio-x" SQLite e encontra ficheiros
e pastas duplicados — dentro de uma drive ou entre várias, mesmo quando já
não estão ligadas.

## Método

1. **Indexar (`index`)** — percorre o sistema de ficheiros e regista para cada
   ficheiro: `caminho`, `tamanho`, `mtime`, `partial_hash`. O *partial hash* é
   um BLAKE2b dos primeiros 64 KiB + últimos 64 KiB + tamanho. Tempo
   ~constante por ficheiro, suficiente para distinguir conteúdos diferentes do
   mesmo tamanho na prática.
   - Symlinks são registados mas não seguidos.
   - Diretórios de sistema (`.Spotlight-V100`, `.Trashes`, `.fseventsd`,
     `.DocumentRevisions-V100`, `$RECYCLE.BIN`, etc.) são saltados.
   - Cada drive produz um `.db` próprio (SQLite, portátil).

2. **Encontrar duplicados (`dedupe`)** — estratégia híbrida em duas fases:
   - **Filtro rápido**: agrupa ficheiros por `(tamanho, partial_hash)`. Grupos
     com 1 elemento ficam imediatamente excluídos.
   - **Confirmação**: para grupos candidatos, lê o ficheiro inteiro e calcula
     BLAKE2b completo. Só ficheiros com `full_hash` igual são reportados.
   - **Pastas duplicadas**: hash de Merkle bottom-up. Para cada diretório,
     ordena os filhos por nome e combina `(tipo, nome, hash)` num BLAKE2b.
     Duas pastas com o mesmo hash têm exatamente o mesmo conteúdo recursivo,
     independentemente do nome da pasta.

3. **Comparar drives (`compare`)** — usa os `.db` directamente, sem precisar
   que as drives estejam montadas. Cruza ficheiros por `(tamanho,
   partial_hash)`. Se ambos os lados tiverem `full_hash`, o match é
   *confirmado* (`=`); caso contrário fica como *quase certo* (`≈`).

## Instalação

Python 3.10+. A CLI (`drive_xray.py`) não tem dependências externas; usa
`hashlib`, `sqlite3` e `pathlib` da stdlib. A UI em Streamlit (`app.py`)
requer o pacote `streamlit`, instalado num virtualenv isolado.

```bash
# tornar a CLI executável
chmod +x ~/tools/drive-xray/drive_xray.py

# venv para a UI Streamlit (já feito; recriar se for preciso)
python3 -m venv ~/tools/drive-xray/.venv
~/tools/drive-xray/.venv/bin/pip install --upgrade pip
~/tools/drive-xray/.venv/bin/pip install streamlit
```

Opcional — alias no `~/.zshrc`:

```bash
alias dx='python3 ~/tools/drive-xray/drive_xray.py'
alias dx-ui='~/tools/drive-xray/.venv/bin/streamlit run ~/tools/drive-xray/app.py'
```

## Utilização

### Interface gráfica (Streamlit)

Duas formas de arrancar a UI:

**(a) `.app` clicável** (mais confortável):

```bash
bash ~/tools/drive-xray/build_app.sh
# Cria ~/Applications/drive-xray.app — double-click no Finder
# ou:   open ~/Applications/drive-xray.app
# ou:   arrastar para a Dock para futuro acesso
```

Para usar uma porta diferente: `DRIVE_XRAY_PORT=8888 open ~/Applications/drive-xray.app`.
Logs em `~/Library/Logs/drive-xray.log`.
Para parar: clique direito no ícone da Dock → Quit. (O Streamlit não
fecha sozinho quando o browser fecha.)

**(b) Linha de comandos:**

```bash
~/tools/drive-xray/.venv/bin/streamlit run ~/tools/drive-xray/app.py
```

Em ambos os casos abre automaticamente em `http://localhost:8501`. A UI tem:

- **Sidebar** — lista das drives indexadas em `~/tools/drive-xray/*.db` e
  formulário para indexar uma nova (caminho + etiqueta + `--full`
  opcional). O `index` corre como subprocesso com log ao vivo e botão
  **Cancelar**.
- **📊 Resumo** — contagens, tamanho total, top 20 extensões por ocupação.
- **🔁 Duplicados** — slider de tamanho mínimo, lista de grupos de
  ficheiros e de pastas duplicadas, ordenados por espaço desperdiçado.
  Avisa se a drive original não estiver montada.
- **⚖️ Comparar** — cruza duas `.db`, tabela com tag `=` (confirmado por
  full hash) ou `≈` (match provável por size + partial), métricas no topo.

Para parar a UI: `Ctrl-C` no terminal onde a lançaste.

### CLI

#### Indexar uma drive

```bash
# Mac (HOME) — sem externos (fora de ~), e sem clouds sync
dx index ~/ --label mac-home -x --skip-cloud

# Disco interno completo — usa -x para não atravessar para /Volumes
# nem duplicar via firmlinks do APFS (ver "Notas para macOS" abaixo)
dx index / --label mac-root -x --skip-cloud

# Drive externa — -x impede contaminação se houver outros volumes ligados
dx index /Volumes/Backup2024 --label backup-2024 -x

# Indexação completa com hash de tudo (lento, mas permite confirmar
# comparações offline)
dx index /Volumes/Backup2024 --label backup-2024 -x --full
```

Output: `~/tools/drive-xray/<label>.db`.

**Flags:**
- `--full` — calcula BLAKE2b completo de cada ficheiro durante a indexação.
  Lento, mas necessário para `compare` confirmado quando a drive não está
  montada.
- `-x` / `--one-filesystem` — não atravessa mount points. Análogo ao
  `find -xdev` ou `rsync -x`. Cada drive externa fica num índice próprio
  sem contaminação cruzada.
- `--skip-cloud` — ignora pastas de sincronização cloud: **iCloud Drive**
  (`~/Library/Mobile Documents`), **OneDrive** (incluindo
  `OneDrive - <Tenant>`), **Google Drive**, **Dropbox**, **Box**,
  **MEGA**, **pCloud**, **Tresorit**, **Proton Drive**, e tudo o que
  estiver em `~/Library/CloudStorage/` (hub do macOS Monterey+). Evita
  *download* acidental de ficheiros "só online" e exclui conteúdo que
  não é realmente "da drive".

#### Encontrar duplicados numa drive

```bash
dx dedupe ~/tools/drive-xray/backup-2024.db
dx dedupe ~/tools/drive-xray/backup-2024.db --min-size 1048576   # ignorar <1 MB
dx dedupe ~/tools/drive-xray/backup-2024.db --dirs-only
dx dedupe ~/tools/drive-xray/backup-2024.db --files-only
```

A drive tem de estar montada (precisa ler ficheiros para o hash completo).
Se já tiveres indexado com `--full`, podes correr `dedupe` sem a drive
ligada — só não vai conseguir confirmar candidatos novos.

#### Snapshots temporais (`snapshot`, `diff`, `prune`)

Cada `.db` mantém um histórico de **snapshots** (estados imutáveis da
drive ao longo do tempo). `refresh` sobrescreve o snapshot mais recente
no sítio; `snapshot take` cria um novo, preservando todos os anteriores.

```bash
# Tirar novo snapshot (preserva o histórico anterior)
dx snapshot take ~/tools/drive-xray/mynas.db
dx snapshot take ~/tools/drive-xray/mynas.db --full
dx snapshot take ~/tools/drive-xray/mynas.db --no-prune  # desliga retenção auto

# Listar snapshots
dx snapshot list ~/tools/drive-xray/mynas.db
#   3 snapshot(s) in mynas.db:
#   #  3  2025-06-15T18:23:11   5,301,234 files   20.9 TB   mynas
#   #  2  2025-06-01T12:00:08   5,123,891 files   20.4 TB   mynas
#   #  1  2025-05-01T09:14:55   4,987,000 files   19.6 TB   mynas

# Diferença entre snapshots (default: penúltimo → último)
dx diff ~/tools/drive-xray/mynas.db
dx diff ~/tools/drive-xray/mynas.db --from 1 --to 3 --top 20

# Aplicar política de retenção manualmente
dx prune ~/tools/drive-xray/mynas.db --keep-last 10 --keep-monthly 12
```

**Política de retenção** (default automático após cada `snapshot take`):
mantém os **10 snapshots mais recentes** + **um por mês dos últimos 12
meses**. Drives de produção semanais ficam com ~22 snapshots no total.
Para desativar, usa `--no-prune` ou `--keep-last 0 --keep-monthly 0`.

**Output do `diff`:**

```
=== diff: snapshot #2 (2025-06-01) → #3 (2025-06-15) ===

  +   178,343 files  +487.3 GB
  −     4,121 files  −12.1 GB
  ~    23,045 modified  (size Δ +52.0 GB)
  ─────────────────────────────────
  net size change: +527.2 GB

Top folders by growth:
  + 412 GB   sequencing/run-2025-06/
  +  53 GB   projects/raw_signal/
  +  18 GB   backups/weekly/
```

**Diferença `refresh` vs `snapshot`:**

| Operação | Histórico | Quando usar |
|---|---|---|
| `dx refresh <db>` | sobrescreve o último snapshot | quando só queres saber o estado actual |
| `dx snapshot take <db>` | cria um novo (preserva os anteriores) | para acompanhar evolução ao longo do tempo |

Na UI Streamlit:
- Sidebar tem botões **📸** (snapshot) e **🔄** (refresh) por drive.
- Tab **📅 Histórico** mostra tabela de todos os snapshots da drive
  seleccionada + selectores de "De / Até" para correr o `diff` visualmente
  (métricas no topo + top pastas por crescimento / redução).

#### Re-indexação incremental (`refresh`)

```bash
dx refresh ~/tools/drive-xray/backup-2024.db          # mantém opções originais
dx refresh ~/tools/drive-xray/backup-2024.db --full   # aproveita para calcular full hash
```

Re-percorre a drive original. Ficheiros cujo `(rel_path, size, mtime)` não
mudou **reutilizam** o `partial_hash` e o `full_hash` da indexação anterior
(zero IO de hash). Útil para drives grandes onde uma re-indexação do zero
demoraria horas. As opções `--one-filesystem` / `--skip-cloud` são herdadas
da indexação inicial (estão guardadas em `drive.opt_one_fs` e
`drive.opt_skip_cloud`).

**Tolerância de mtime**: 1 segundo, para acomodar HFS+ (resolução 1s) vs
APFS (1ns). Edições que preservem mtime (raro fora de `rsync --times`) não
são detetadas.

#### Exportar duplicados (`export`)

```bash
dx export ~/tools/drive-xray/rleite.db dups.csv  --min-size 10485760
dx export ~/tools/drive-xray/rleite.db dups.xlsx --min-size 10485760
```

Output tem uma linha por ficheiro com: `group_id`, `hash`, `size_bytes`,
`size_human`, `group_count`, `distinct_inodes`, `wasted_bytes`,
`wasted_human`, `path`, `is_hardlink`. XLSX precisa de `openpyxl` no venv
(já instalado).

Na UI Streamlit, a tab **🔁 Duplicados** tem botões **⬇️ Exportar CSV** e
**⬇️ Exportar Excel** que descarregam diretamente pelo browser.

#### Comparar duas drives

```bash
dx compare \
    ~/tools/drive-xray/mac-home.db \
    ~/tools/drive-xray/backup-2024.db \
    --min-size 1048576
```

- `=` match confirmado por full hash em ambos os lados
- `≈` match provável (mesmo tamanho + partial hash; sem confirmação)
- mostra também contagem de ficheiros que só existem em A

#### Limpeza assistida (`cleanup`)

```bash
# default: keep shortest path, move others to quarantine, ≥1 MB
dx cleanup ~/tools/drive-xray/rleite.db -o plan.sh

# variantes
dx cleanup rleite.db --strategy oldest --action delete --min-size 10485760
dx cleanup rleite.db --strategy alphabetical --action quarantine
```

**Não apaga nada.** Gera um script `.sh` que tu revês e corres à mão. Para
cada grupo de duplicados:

- escolhe uma cópia para **KEEP** segundo a estratégia (`shortest`,
  `oldest`, `newest`, `alphabetical`),
- emite `rm` ou `mv "$QUARANTINE/..."` para as outras cópias com **inode
  distinto**,
- marca **hardlinks** como comentário — apagá-los não liberta espaço real
  enquanto o inode tiver outras referências.

Modo `quarantine` move para `~/.drive-xray-quarantine/<label>-<timestamp>/`
com nomes únicos (`g0042_i1__path__to__file`), preservando a possibilidade
de recuperar antes de esvaziar a quarentena.

Resumo no fim do script: nº de acções, espaço recuperável estimado, nº de
hardlinks notados.

Na UI Streamlit, secção **🧽 Assistente de limpeza** na tab Duplicados:
escolhe estratégia + acção, gera o plano, descarrega o `.sh`.

#### Treemap de utilização (UI)

Tab **🗺️ Mapa** — visualização Plotly do espaço por pasta. Slider de
tamanho mínimo (default 100 MB) controla a granularidade. Click para
drill-in. Ficheiros individuais opcionais. Cobre o caso de uso do
WizTree/GrandPerspective sem sair da UI.

#### Compactar uma `.db` (`compact`)

```bash
dx compact ~/tools/drive-xray/rleite.db
```

Equivale a `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM`. Liberta páginas
fragmentadas e zera o ficheiro `.db-wal`. Sem perda de dados. Também
dispara a **migração de schema v2 → v3** se a `.db` for antiga (ver abaixo).

Na UI: tab **📊 Resumo** → métrica **Tamanho .db** + botão **🧹 Compactar**.

## Schema v3 (compactação)

Desde o schema v3 a tabela `entries` é mais leve:

- `partial_hash` e `full_hash` em **BLOB** (16 / 32 bytes raw, antes eram hex
  de 32 / 64 chars).
- Coluna `name` removida (deriva-se de `basename(rel_path)` quando preciso).
- `parent_path` (TEXT) substituído por `parent_id` (INTEGER, FK para
  `entries.id`) — índice `idx_parent` muito mais pequeno e joins mais rápidos.

**Migração automática**: `.db` em schema antigo (v1/v2) são detetadas e
migradas na primeira abertura via `open_db`. A migração:

- Converte hex → BLOB.
- Mapeia `parent_path` → `parent_id` via lookup por `rel_path`.
- `partial_hash = "EMPTY"` (string antiga) → `BLAKE2b(size=0)` determinístico.
- `partial_hash = "ERR:N"` → NULL (a mensagem fica na coluna `error`).
- Preserva todos os `id`s e referências entre entradas.

Para forçar a migração + compactação física: `dx compact <db>`.

## Estrutura do projeto

```
~/tools/drive-xray/
  drive_xray.py     # CLI + funções de indexação/dedup/compare
  app.py            # UI Streamlit
  README.md
  .venv/            # virtualenv com streamlit (ignorar)
  <label>.db        # um SQLite por drive indexada
```

## Esquema da base de dados

```sql
drive(label, root_path, indexed_at, total_files, total_dirs, total_size)
entries(rel_path, parent_path, name, is_dir, size, mtime,
        partial_hash, full_hash, is_symlink, error)
```

Podes consultar diretamente com `sqlite3 backup-2024.db`.

## Notas para macOS

- **Discos externos** montam em `/Volumes/<nome>`. Indexar `/` sem `-x`
  vai apanhar **todos** os discos ligados na mesma `.db`. Usa `-x` ou
  indexa cada drive a partir do seu próprio mount point.
- **Firmlinks do APFS**: em macOS moderno (Big Sur+), `/` é a volume de
  sistema (read-only) e os teus dados vivem em `/System/Volumes/Data`,
  unidos por *firmlinks* (que parecem pastas normais). Indexar `/` sem
  `-x` percorre os teus ficheiros **duas vezes**. Com `-x`, o walker
  detecta que `/Users`, `/Applications`, etc., estão noutro volume e
  prune-os — para indexar tudo o que é teu, prefere `dx index ~/ -x` ou
  `dx index /System/Volumes/Data -x --label mac-data`.
- O log mostra `[pruned N cross-fs subtrees]` no fim quando usaste `-x`,
  para confirmares que algum corte aconteceu.

## Usar a mesma drive em vários sistemas (macOS ↔ Windows ↔ Linux)

O `root_path` fica gravado na `.db` tal como estava na altura da indexação
(`/Volumes/MeuDisco` no macOS, `E:\` no Windows, `/media/<user>/MeuDisco`
no Linux). O dx valida o caminho gravado e, se necessário, procura o volume
nos mount points da plataforma actual, usando uma impressão digital por
conteúdo: um candidato só é aceite com a maioria dos nomes de topo do
último snapshot presentes (mínimo 2 quando há 2+) **e** pelo menos um dos
maiores ficheiros indexados presente no mesmo caminho relativo com o
tamanho exacto em bytes. Nomes genéricos ("Photos", "Backup") não chegam
para um falso positivo, e um disco diferente que tenha ocupado o caminho
antigo (`E:\` reatribuído, `/Volumes/Nome` reutilizado) é detectado — o
volume verdadeiro é procurado noutro mount point. Não depende do
nome/label do volume (o exFAT põe labels em maiúsculas e o Windows nem
sequer o expõe no caminho). Isto aplica-se a `refresh`, `snapshot`,
`dedupe`, `export`, scripts de cleanup/backup, verificação de integridade
e aos avisos de "drive não montada" na UI. Sem correspondência confiante
em lado nenhum, o comportamento é o de sempre: a drive é tratada como não
montada. Nota: drives de rede/UNC não são pesquisadas — o caminho gravado
é usado tal e qual.

## Limitações conhecidas

- **APFS clones** (cópias copy-on-write, `cp -c`): aparecem como ficheiros
  com inodes distintos mas que partilham blocos físicos. São reportados
  como duplicados — o que está tecnicamente correcto a nível de conteúdo,
  mas o "espaço desperdiçado" calculado é maior do que o real. Detetar
  clones requer syscalls específicas que ainda não estão implementadas.
- **Resource forks / xattrs do macOS** não fazem parte do hash. Dois
  ficheiros com conteúdo idêntico mas xattrs diferentes (ex.: tags do
  Finder) são considerados duplicados.
- **Pastas duplicadas aninhadas** são reportadas em todos os níveis. Se
  `a/` e `b/` são idênticas, vais ver `a/`==`b/` **e** `a/sub`==`b/sub`.
  Para limpeza, foca-te no nível mais alto.
- **Renames preservando conteúdo**: `refresh` deteta o ficheiro como
  removido + novo, e re-hasha. Não é grave, mas perde-se cache. Mover
  pastas inteiras tem o mesmo custo.

## Roadmap

### Entregue

- **Hashing híbrido** v2 — partial (head + middle + tail) → full sob
  demanda. BLAKE2b, 16 / 32 bytes raw em BLOB.
- **Merkle hash** de pastas (bottom-up sobre `parent_id`).
- **Filtros macOS** — `-x` (firmlinks + cross-mount), `--skip-cloud`
  (iCloud / OneDrive / Google Drive / Dropbox / Box / MEGA / Proton).
- **Schema v3 compactado** — BLOB hashes, `parent_id` em vez de
  `parent_path`, migração automática de `.db` antigas.
- **Refresh incremental** — reutiliza hashes de ficheiros com `(size,
  mtime)` inalterado.
- **Detecção de hardlinks** via `(inode, device)` — não inflam o
  "wasted".
- **Export CSV/XLSX** dos grupos de duplicados (`openpyxl`).
- **TreeMap** Plotly por pasta, drill-in interactivo, slider de
  threshold.
- **Cleanup assistido** — gera script `bash` (`rm` ou `mv` para
  quarentena) com 4 estratégias de keeper. Nunca apaga directamente.
- **UI Streamlit** bilingue PT/EN, gestão de drives (selecção / refresh
  / compact / delete), download buttons.
- **Compact** — `VACUUM` + `PRAGMA wal_checkpoint(TRUNCATE)`.
- **Snapshots temporais** — schema v4 com tabela `snapshots`; `dx snapshot
  take` cria novo (preserva histórico), `dx diff` compara dois snapshots
  mostrando added/removed/modified + top pastas por crescimento.
  Retenção automática: 10 mais recentes + 12 mensais.

### Em aberto

**Tier 3 — Path interning**
Para drives de 4M+ ficheiros o autoindex sobre `rel_path` (a UNIQUE
constraint) é o maior componente da `.db` (≥1 GB). Solução: tabela
`paths(id, parent_id, segment)` referenciada por `entries.path_id`.
Reconstrução do caminho via CTE recursiva. Reduz mais ~30-40 %. Custo:
1 join recursivo em queries que mostram path absoluto.

**Empacotamento como app nativa macOS**
- `py2app` ou `PyInstaller` → `.app` distribuível com Python embebido.
- Alternativa polida: **Tauri** com webview a apontar para o Streamlit
  embebido, ou substituir Streamlit por Svelte/React + backend FastAPI.
- Code-signing + notarização Apple para correr sem avisos do Gatekeeper.

**Snapshots V2 — content-addressing**
Cada snapshot guarda hoje a tabela de entries completa (~150 bytes/row).
Para drives muito grandes (5M+ ficheiros) com snapshots semanais, isto
escala para ~40 GB/ano. Solução: tabela `file_state(id, size, mtime,
partial_hash, full_hash)` partilhada entre snapshots, `entries` ganha
`state_id` e referência o estado real. Para drives com baixa churn
(típico em bio: dados read-only), reduz ~80 %. Combina naturalmente com
Tier 3 (path interning).

**Cleanup v2 — execução in-place**
Botão "Executar plano" na UI com confirmação dupla (`escrever SIM`),
progress bar e quarentena pré-criada antes de qualquer `mv`. Hoje a UI
só gera o script; ainda tens de o correr à mão. Esta evolução mantém a
quarentena como passo intermédio mas remove o atrito.

**Detecção de APFS clones**
Usar `clonefile()` syscall ou `getattrlist` com `ATTR_CMNEXT_FNDRINFO`
para detectar blocos partilhados entre inodes distintos. Permite
calcular o espaço **realmente** recuperável em macOS APFS, em vez de
sobrestimar.

### Rust port

🎉 **Sprints 1-5 entregues.** O binário `dx` em [`rust/`](rust/) é
drop-in compatível com a CLI Python (mesmos subcommandos, mesma `.db`
v4 byte-a-byte) e ~10× mais rápido em `index`/`snapshot`.

Estado:

| Sprint | Conteúdo | Estado |
|---|---|---|
| 1 | Schema + migrações v1→v4 | ✅ |
| 2 | Hash BLAKE2b + walker + index | ✅ — 11.5× sobre Python |
| 3 | Snapshot family (take/list/prune/diff) | ✅ |
| 4 | Read ops (dedupe/compare/export/cleanup/compact) | ✅ — 6× dedupe |
| 5 | Parity suite + universal binary + app.py integration | ✅ — 31 tests verdes |

**Para usar:**

```bash
# build (uma vez)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
cd ~/tools/drive-xray/rust
rustup target add aarch64-apple-darwin x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin
cargo build --release --target x86_64-apple-darwin
lipo -create -output target/universal/dx \
    target/aarch64-apple-darwin/release/dx \
    target/x86_64-apple-darwin/release/dx

# verificar — a UI Streamlit detecta automaticamente
~/tools/drive-xray/.venv/bin/streamlit run ~/tools/drive-xray/app.py
# sidebar mostrará "engine: 🦀 Rust"
```

A app procura o binário nesta ordem: `$DRIVE_XRAY_DX` env var →
`rust/target/{universal,release}/dx` → `dx` no PATH → fallback Python.

Detalhes técnicos completos: [`rust/DESIGN.md`](rust/DESIGN.md).



Motivação: o walker e o hashing são os bottlenecks absolutos. Em 30 TB
a indexação inicial em Python anda na ordem das 4-8 h (limitado por IO
~200 MB/s + GIL nos workers de hashing). Há ~5-10× a ganhar.

**Fase A — Core Rust como binário CLI**
- Reescrever `index`, `dedupe`, `refresh`, `compact`, `export`,
  `cleanup` em Rust.
- **BLAKE3** substitui BLAKE2b: SIMD nativo (x86_64 + Apple Silicon),
  ~3× mais rápido em ficheiros grandes, qualidade criptográfica
  equivalente. Tree mode do BLAKE3 alinha-se naturalmente com o nosso
  Merkle de pastas.
- `walkdir` + `rayon` para paralelismo (directório + hashing
  concorrente, sem GIL).
- `rusqlite` mantém o **mesmo schema v3** — `.db` produzidas pelo Rust
  binário continuam a abrir na UI Python e vice-versa.
- A UI Streamlit chama o binário via subprocess (já é o padrão para
  `index` / `refresh` / `compact`). **Zero alterações para o utilizador
  excepto o tempo.**
- Distribuição: `cargo install drive-xray` + um Homebrew tap, ou bundle
  no `.app` (ver acima).

**Fase B — Hot paths em Rust via PyO3**
- `dup_file_groups`, `dup_folder_groups`, `treemap_rows` migram para
  módulos PyO3 expostos como wheel. A UI Streamlit consome-os
  directamente — sem subprocess, sem serialização.
- Reduz o tempo de "Procurar duplicados" em 4M ficheiros de ~3 min
  para <30 s.

**Fase C — UI nativa**
- Substituir Streamlit por **Tauri** (Rust + webview) com front-end em
  Svelte/React. Bundle de ~30 MB, sem dependência de Python no
  utilizador final.
- `.app` code-signed + notarized, distribuível como DMG.

**Risco principal**: paridade funcional + garantia bit-a-bit com o
output Python (mesmo hash, mesmas decisões de Merkle). Mitigação:
manter uma test suite de regressão que corra o binário Rust e o script
Python contra os mesmos directórios sintéticos e compare as `.db`
resultantes.

**Cronograma realista** (1 dev part-time): Fase A em 3-4 semanas; Fase
B em 2 semanas; Fase C em 4-6 semanas. Não vale a pena começar Fase B
sem A consolidada e perfilada.

**Re-ordenação após Sprints 1-4** (já com o binário Rust funcional):

- **Fase A subprocess** absorve ~95 % do valor — o `app.py` mantém-se
  igual e troca-se 1 linha para preferir o binário Rust quando
  presente. Não há pressão para PyO3 enquanto as queries da UI ficarem
  em ~1 s.
- **Fase B PyO3 é cirúrgica** — só onde a medição mostrar dor (e.g.
  `compute_dir_hashes` em snapshots gigantes, `treemap_rows` em
  árvores muito profundas). PyO3 traz custo de matriz de build
  (Python ABI × arch × OS) que subprocess não tem.
- **Fase C Tauri** é o "depois de gostarmos do produto" — para os
  nichos-alvo (bio / labs / fotógrafos / NAS owners), Streamlit serve.

Notas técnicas críticas para a transição:

- **mtime byte-equivalence** — verificada empiricamente: a fórmula
  CPython `tv_sec + tv_nsec * 1e-9` está implementada de forma
  bit-idêntica em [`rust/src/walker.rs`](rust/src/walker.rs), com
  test de regressão em `tests/db_parity.rs`. SQLite `REAL` preserva
  o IEEE 754 round-trip.
- **inode/device wrap u64→i64** para exFAT/NTFS/APFS com IDs altos —
  já tratado em ambas as implementações.
- **Hashes BLAKE2b byte-idênticas** entre Python (`hashlib`) e Rust
  (`blake2b_simd`) — validado com vectores golden em
  `tests/hash::tests`.
