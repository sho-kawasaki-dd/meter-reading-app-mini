from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_qt_charts_draws_offscreen() -> None:
    script = textwrap.dedent(
        """
        from PySide6.QtCharts import QChart, QChartView, QLineSeries
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPen
        from PySide6.QtWidgets import QApplication

        app = QApplication([])
        series = QLineSeries()
        series.setPen(QPen(QColor("red"), 5))
        series.append(0, 1)
        series.append(1, 2)

        chart = QChart()
        chart.legend().hide()
        chart.addSeries(series)
        chart.createDefaultAxes()
        view = QChartView(chart)
        view.resize(480, 320)
        view.show()
        app.processEvents()

        image = QImage(view.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        view.render(painter)
        painter.end()
        assert not image.isNull()
        assert any(
            (color := image.pixelColor(x, y)).red() > 180
            and color.green() < 120
            and color.blue() < 120
            and color.alpha() > 0
            for x in range(image.width())
            for y in range(image.height())
        ), "グラフの系列が描画されていません"
        """
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
