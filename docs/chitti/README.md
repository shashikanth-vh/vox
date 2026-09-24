# Chitti documentation

## Architecture

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the services/Chitti HTTPS topology, certificate and configuration guidance, two-instance deployment, and readiness checks. [ARCHITECTURE.html](ARCHITECTURE.html) embeds all three PNGs and can be opened without external assets. Deployment examples use generic domains and describe checked-in configuration, not a verified live deployment.

## Publishing to Confluence

Copy the rendered document into the Confluence editor. Attach and insert `images/system-context.png`, `images/request-flow.png`, and `images/deployment.png` in their respective sections if the editor does not preserve embedded images when pasting. Source references are plain text and need no file links.

## Editing and regeneration

Each diagram has an editable SVG and a PNG for publishing in `images/`. `render_architecture.py` defines the diagram layout and generates both formats plus the HTML document. For changes that should survive regeneration, update the script. Direct SVG edits are overwritten when it runs.

With Pillow and Python-Markdown available, run from the repository root:

```bash
python3 docs/chitti/render_architecture.py
```

The renderer uses the system DejaVu Sans font at `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`.
