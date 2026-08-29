"""Server-rendered inline SVG charts.

Charts are built as SVG strings on the server for two reasons. There is no
JavaScript and no CDN dependency, and the browser never talks to S3 directly,
so the report bucket needs no CORS grant and stays fully private.

Every caller-supplied string is escaped here. Chart labels are derived from
CloudTrail data and model output, which are untrusted.
"""

from __future__ import annotations

from html import escape

# Palette matches templates/report.html.j2.
INK = "#1a2330"
MUTED = "#5b6b7f"
LINE = "#e2e8f0"
ACCENT = "#1f6feb"
DANGER = "#b42318"
WARN = "#b54708"
OK = "#067647"

CATEGORY_COLORS = {
    "DEPLOYMENT": "#1f6feb",
    "FAILED_DEPLOYMENT": "#b42318",
    "RESOURCE_LIFECYCLE": "#7c3aed",
    "IAM_POLICY": "#b54708",
    "NETWORK": "#0e7490",
    "SECURITY": "#be123c",
    "FAILED_OPERATION": "#dc2626",
    "OTHER": "#64748b",
}


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _nice_ceiling(value: int) -> int:
    """Round a max value up to something that makes a readable axis."""
    if value <= 5:
        return 5
    step = 10 ** (len(str(value)) - 1)
    return ((value // step) + 1) * step


def empty_chart(message: str, width: int = 720, height: int = 220) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_e(message)}">'
        f'<rect width="{width}" height="{height}" fill="#fff" rx="10"/>'
        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
        f'font-size="13" fill="{MUTED}">{_e(message)}</text></svg>'
    )


def grouped_bars(
    labels: list[str],
    series: list[tuple[str, str, list[int]]],
    *,
    width: int = 720,
    height: int = 260,
    title: str = "",
) -> str:
    """Grouped vertical bar chart.

    ``series`` is a list of ``(name, colour, values)``. ``values`` must be the
    same length as ``labels``.
    """
    if not labels or not series:
        return empty_chart("No history yet.", width, height)

    pad_left, pad_right, pad_top, pad_bottom = 44, 12, 28, 42
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    peak = max((max(values) if values else 0) for _, _, values in series)
    axis_max = _nice_ceiling(peak)

    slot = plot_w / len(labels)
    bar_w = max(2.0, min(18.0, (slot * 0.7) / len(series)))
    group_w = bar_w * len(series)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_e(title or "chart")}">',
        f'<rect width="{width}" height="{height}" fill="#fff" rx="10"/>',
    ]

    # Horizontal gridlines and y-axis labels.
    for tick in range(5):
        value = axis_max * tick / 4
        y = pad_top + plot_h - (plot_h * tick / 4)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" '
            f'y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="10" fill="{MUTED}">{int(value)}</text>'
        )

    # Bars.
    for index, label in enumerate(labels):
        group_x = pad_left + slot * index + (slot - group_w) / 2
        for offset, (name, colour, values) in enumerate(series):
            value = values[index] if index < len(values) else 0
            bar_h = 0.0 if axis_max == 0 else (value / axis_max) * plot_h
            bar_h = min(bar_h, plot_h)
            x = group_x + offset * bar_w
            y = pad_top + plot_h - bar_h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 1:.1f}" '
                f'height="{max(bar_h, 0.8):.1f}" fill="{colour}" rx="1.5">'
                f"<title>{_e(label)} — {_e(name)}: {value}</title></rect>"
            )

    # X labels, thinned so they never collide.
    every = max(1, len(labels) // 12)
    for index, label in enumerate(labels):
        if index % every:
            continue
        x = pad_left + slot * index + slot / 2
        short = label[5:] if len(label) == 10 else label  # MM-DD
        parts.append(
            f'<text x="{x:.1f}" y="{height - pad_bottom + 16}" '
            f'text-anchor="middle" font-size="10" fill="{MUTED}">{_e(short)}</text>'
        )

    # Legend.
    legend_y = height - 8
    legend_x = pad_left
    for name, colour, _ in series:
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 8}" width="9" height="9" '
            f'fill="{colour}" rx="2"/>'
        )
        parts.append(
            f'<text x="{legend_x + 13}" y="{legend_y}" font-size="10" '
            f'fill="{MUTED}">{_e(name)}</text>'
        )
        legend_x += 22 + 6 * len(name)

    parts.append("</svg>")
    return "".join(parts)


def stacked_bars(
    labels: list[str],
    stacks: list[dict[str, int]],
    colors: dict[str, str],
    *,
    width: int = 720,
    height: int = 240,
    title: str = "",
) -> str:
    """Stacked bars, one stack per label. Used for episodes by category."""
    if not labels or not stacks:
        return empty_chart("No episodes recorded yet.", width, height)

    pad_left, pad_right, pad_top, pad_bottom = 44, 12, 28, 42
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    totals = [sum(stack.values()) for stack in stacks]
    axis_max = _nice_ceiling(max(totals) if totals else 0)
    slot = plot_w / len(labels)
    bar_w = max(3.0, min(26.0, slot * 0.62))

    ordered_keys = sorted({key for stack in stacks for key in stack})

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_e(title or "chart")}">',
        f'<rect width="{width}" height="{height}" fill="#fff" rx="10"/>',
    ]

    for tick in range(5):
        value = axis_max * tick / 4
        y = pad_top + plot_h - (plot_h * tick / 4)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" '
            f'y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="10" fill="{MUTED}">{int(value)}</text>'
        )

    for index, label in enumerate(labels):
        stack = stacks[index]
        x = pad_left + slot * index + (slot - bar_w) / 2
        cursor = pad_top + plot_h
        for key in ordered_keys:
            value = stack.get(key, 0)
            if value <= 0:
                continue
            seg_h = 0.0 if axis_max == 0 else (value / axis_max) * plot_h
            cursor -= seg_h
            colour = colors.get(key, CATEGORY_COLORS.get(key, MUTED))
            parts.append(
                f'<rect x="{x:.1f}" y="{cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{max(seg_h, 0.8):.1f}" fill="{colour}">'
                f"<title>{_e(label)} — {_e(key)}: {value}</title></rect>"
            )

    every = max(1, len(labels) // 12)
    for index, label in enumerate(labels):
        if index % every:
            continue
        x = pad_left + slot * index + slot / 2
        short = label[5:] if len(label) == 10 else label
        parts.append(
            f'<text x="{x:.1f}" y="{height - pad_bottom + 16}" '
            f'text-anchor="middle" font-size="10" fill="{MUTED}">{_e(short)}</text>'
        )

    legend_x, legend_y = pad_left, height - 8
    for key in ordered_keys:
        colour = colors.get(key, CATEGORY_COLORS.get(key, MUTED))
        label = key.replace("_", " ").title()
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 8}" width="9" height="9" '
            f'fill="{colour}" rx="2"/>'
        )
        parts.append(
            f'<text x="{legend_x + 13}" y="{legend_y}" font-size="10" '
            f'fill="{MUTED}">{_e(label)}</text>'
        )
        legend_x += 22 + 5 * len(label)

    parts.append("</svg>")
    return "".join(parts)


def signal_bar(collected: int, included: int, *, width: int = 720) -> str:
    """A single horizontal bar showing kept vs filtered events."""
    height = 58
    if collected <= 0:
        return empty_chart("No events collected yet.", width, height)

    kept_frac = max(0.0, min(1.0, included / collected))
    pad = 12
    bar_w = width - 2 * pad
    kept_w = bar_w * kept_frac
    dropped = max(0, collected - included)

    return "".join(
        [
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="Signal versus noise">',
            f'<rect width="{width}" height="{height}" fill="#fff" rx="10"/>',
            f'<rect x="{pad}" y="14" width="{bar_w}" height="18" fill="{LINE}" rx="4">'
            f"<title>Filtered out: {dropped}</title></rect>",
            f'<rect x="{pad}" y="14" width="{kept_w:.1f}" height="18" '
            f'fill="{ACCENT}" rx="4"><title>Kept for analysis: {included}</title></rect>',
            f'<text x="{pad}" y="47" font-size="11" fill="{MUTED}">'
            f"{included} kept for analysis</text>",
            f'<text x="{width - pad}" y="47" text-anchor="end" font-size="11" '
            f'fill="{MUTED}">{dropped} filtered as noise</text>',
            "</svg>",
        ]
    )
