from __future__ import annotations

import streamlit.components.v1 as components


def enable_plotly_daily_keyboard_navigation() -> None:
    components.html(
        """
        <script>
        (() => {
            const doc = window.parent.document;
            const win = window.parent;
            const DAY_MS = 24 * 60 * 60 * 1000;

            const isTypingTarget = (target) => {
                if (!target) return false;
                const tag = target.tagName ? target.tagName.toLowerCase() : "";
                return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
            };

            const plots = () => Array.from(doc.querySelectorAll(".js-plotly-plot"));

            let activePlot = plots().at(-1) || null;
            plots().forEach((plot) => {
                plot.setAttribute("tabindex", "0");
                plot.addEventListener("mouseenter", () => { activePlot = plot; });
                plot.addEventListener("focusin", () => { activePlot = plot; });
            });

            if (win.__investmentDashboardPlotlyKeyNav) return;
            win.__investmentDashboardPlotlyKeyNav = true;

            win.addEventListener("keydown", (event) => {
                if (isTypingTarget(event.target)) return;
                if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;

                const Plotly = win.Plotly;
                const plot = activePlot || plots().at(-1);
                if (!Plotly || !plot || !plot._fullLayout || !plot._fullLayout.xaxis) return;

                const axis = plot._fullLayout.xaxis;
                let range = axis.range;
                if (!range || range.length !== 2) {
                    const dataDates = [];
                    (plot.data || []).forEach((trace) => {
                        (trace.x || []).forEach((value) => {
                            const date = new Date(value);
                            if (!Number.isNaN(date.getTime())) dataDates.push(date.getTime());
                        });
                    });
                    if (!dataDates.length) return;
                    range = [new Date(Math.min(...dataDates)), new Date(Math.max(...dataDates))];
                }

                const start = new Date(range[0]).getTime();
                const end = new Date(range[1]).getTime();
                if (Number.isNaN(start) || Number.isNaN(end)) return;

                const step = event.key === "ArrowLeft" ? -DAY_MS : DAY_MS;
                event.preventDefault();
                Plotly.relayout(plot, {
                    "xaxis.range": [
                        new Date(start + step).toISOString().slice(0, 10),
                        new Date(end + step).toISOString().slice(0, 10),
                    ],
                });
            });
        })();
        </script>
        """,
        height=0,
    )
