<div align="center">

<img src="assets/icon.png" alt="drive-xray" width="180"/>

# drive-xray

**Saber exactamente o que tens em cada drive, e o que está duplicado entre elas.**

[![macOS](https://img.shields.io/badge/macOS-11+-black?logo=apple)](https://www.apple.com/macos/)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Rust](https://img.shields.io/badge/Rust-1.75+-orange?logo=rust&logoColor=white)](https://www.rust-lang.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

## Para quem é isto

Isto foi pensado para quem **vive com muitas drives USB espalhadas pela
secretária**, lida com **ficheiros gigantes de bioinformática** (BAMs,
FASTQs, VCFs alinhados), nutre **alguma paixão pela fotografia**
(RAWs, projetos Lightroom, edições) — e quer ter a **certeza do que
está armazenado em cada drive** e **qual a redundância** entre elas.

Tipos de pergunta que isto responde:

- "Já tenho este vídeo backuped, ou só está no SSD interno?"
- "Que ficheiros novos apareceram no NAS desde a última semana?"
- "O backup desta drive antiga ainda é o mesmo conteúdo, ou divergiu?"
- "Se eu apagar tudo na drive externa #3, perco alguma coisa que não
  esteja noutro lado?"
- "Quais são as 10 pastas que mais cresceram no projecto este mês?"

A ideia central é simples: **cada drive ganha um "raio-x" — uma `.db`
SQLite portátil** que sabe que ficheiros lá viviam, qual o tamanho,
quando foram modificados, e o hash de cada um. Depois é só comparar.
A drive pode estar desligada — o raio-x continua a responder.

---

## Captura de ecrã

<div align="center">
<em>(coloca aqui um screenshot da UI quando estiver pronto)</em>
</div>

---

## O que faz

- 🔍 **Indexa drives** (interno + externos) num `.db` SQLite portátil.
  Hashing híbrido (BLAKE2b parcial + completo só onde precisa).
- 🔁 **Encontra duplicados** dentro de uma drive, com detecção de
  hardlinks (não conta cópias virtuais como espaço desperdiçado).
- 📅 **Snapshots históricos** — tira "fotografias" mensais ou semanais e
  vê diff: "+ 487 GB em sequencing/run-2025-06/, − 12 GB em tmp/".
- ⚖️ **Compara duas drives** mesmo offline. "Esta cópia ainda é igual
  ao original? Que ficheiros existem só num lado?"
- 🗺️ **TreeMap interactivo** do espaço por pasta (estilo WizTree /
  GrandPerspective).
- 🧽 **Cleanup assistido** — gera um script `.sh` que tu **revês**
  antes de correr. Quarentena ou delete; nunca apaga sozinho.
- 📊 **Exporta** os duplicados em CSV ou XLSX para abrires no Excel.
- 🦀 **Motor em Rust** opcional para drives grandes — ~10× mais
  rápido a indexar 5 M ficheiros, **mesmo `.db` byte-a-byte**
  compatível.

Defesas específicas para macOS:

- `--one-filesystem` evita atravessar firmlinks do APFS (não conta os
  teus ficheiros duas vezes via `/System/Volumes/Data`).
- `--skip-cloud` ignora pastas iCloud / OneDrive / Google Drive /
  Dropbox / Box / MEGA / Proton — não dispara downloads de ficheiros
  "só online".

---

## Instalação rápida

```bash
git clone https://github.com/<your-username>/drive-xray.git
cd drive-xray
python3 -m venv .venv
.venv/bin/pip install streamlit openpyxl plotly
```

E lançar a UI:

```bash
.venv/bin/streamlit run app.py
```

Ou construir um **launcher .app clicável** (recomendado para uso
diário):

```bash
bash build_app.sh
open ~/Applications/drive-xray.app
```

(O launcher abre automaticamente o browser em http://localhost:8501,
com ícone bonito na Dock e Spotlight.)

### Motor Rust opcional (~10× mais rápido em drives grandes)

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
cd rust
rustup target add aarch64-apple-darwin x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin
cargo build --release --target x86_64-apple-darwin
lipo -create -output target/universal/dx \
    target/aarch64-apple-darwin/release/dx \
    target/x86_64-apple-darwin/release/dx
```

A UI Streamlit detecta automaticamente o binário Rust quando ele
existe — sidebar mostra `engine: 🦀 Rust` em vez de `🐍 Python`. As
`.db` são byte-idênticas, podes alternar entre engines à vontade.

---

## Como funciona (resumo)

1. **Indexação híbrida** — partial hash (head + middle + tail × 64 KB,
   BLAKE2b 128) constante por ficheiro. Full hash (BLAKE2b 256) **só**
   nos candidatos a duplicado. Em 30 TB poupa horas comparado com
   "hash de tudo".
2. **Snapshots** — cada `dx snapshot take` cria um registo imutável.
   `dx diff #2 #5` mostra crescimento e shrink por pasta entre dois
   pontos no tempo.
3. **Schema v5 com path interning** — cada nome de pasta vive uma vez
   na tabela `paths`. Em drives de 5 M ficheiros poupa ~20-25 % do
   tamanho da `.db`.
4. **macOS-aware** — `-x` (firmlinks), `--skip-cloud`, tratamento de
   inodes 64-bit em exFAT/NTFS sem overflow.

Documentação completa: [`DOCS.md`](DOCS.md).
Arquitectura do motor Rust: [`rust/DESIGN.md`](rust/DESIGN.md).

---

## Roadmap

| Estado | Componente |
|---|---|
| ✅ | Hashing híbrido v2 (head+middle+tail) |
| ✅ | Merkle hash de pastas |
| ✅ | macOS firmlinks / cloud sync filters |
| ✅ | Schema v5 com path interning |
| ✅ | Refresh incremental (reaproveita hashes inalterados) |
| ✅ | Snapshots históricos + diff + prune |
| ✅ | TreeMap (Plotly) |
| ✅ | Cleanup assistido (script .sh com quarentena) |
| ✅ | UI Streamlit bilingue (PT/EN) |
| ✅ | Motor Rust (~10× mais rápido) |
| ✅ | `.app` launcher para macOS |
| 🔜 | Snapshots V2 — content-addressed (menos espaço para snapshots semanais) |
| 🔜 | Detecção de APFS clones (`clonefile`) |
| 🔜 | Cleanup v2 — execução in-place na UI |
| 🔜 | Distribuição via Homebrew tap |

---

## License

[MIT](LICENSE) — usa, modifica, redistribui à vontade.
Atribuição é apreciada mas não exigida.

---

<div align="center">
<sub>Built by someone who has 12 USB drives in a drawer and wanted to know what's actually on them.</sub>
</div>
