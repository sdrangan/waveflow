# Presentations

Slide decks about Waveflow and the research it supports. Each deck is a directory:

- `project/deck.json` — the deck index: title, slide order, sections, fonts.
- `project/slides/<id>.html` — one slide per file, a single `<section>` on a 1920×1080 canvas with
  inline styles. Speaker notes are the `<aside>` at the end of each slide.
- `figures/` — the source images the slides use, and `figures.json`, which maps each image's
  `/_blob/<id>` reference in the slides to its file here.

The decks are built as claude.ai Slides artifacts, which render, present and export these files
(to PDF or PowerPoint). To start a new deck, copy a directory, edit the slides, and publish it as a
new artifact, re-uploading the figures it uses.

| Deck | Content |
|---|---|
| [waveflow_umb](waveflow_umb/) | The upper mid-band, the SALSA reconfigurable array, distributed processing for massive arrays, Waveflow, and project ideas |

Visual style (shared by the decks): NYU Violet `#57068C` on a light violet-white `#F7F5FA`, dark
violet `#330662` for statement slides, amber `#B4570F` for anything partial or unproven; Source
Serif 4 for headings, IBM Plex Sans for text, JetBrains Mono for code; the NYU Wireless logo top
right.
