"""桌面端程序入口文件。

这个文件在整个上位机工程中的角色，和嵌入式固件里的 `main.c` 很像：
它负责把各个子模块按正确顺序“装起来并启动”，但它自己不承载复杂业务。

从职责划分上看：
- `main.py` 负责创建 Qt 应用对象并显示主窗口
- `main_window.py` 负责搭界面与组织交互
- `ble_client.py` 负责 BLE 通信
- `protocol.py` 负责协议解析
- `plot_widget.py` 负责实时波形
- `data_logger.py` 负责 CSV 保存

这样的分层设计非常适合教学，因为它把“程序启动”和“业务逻辑”分开了：
读者先理解程序如何启动，再逐个学习通信、协议、界面和绘图模块。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兼容两种启动方式：
# 1. python -m pc_app.main
# 2. python pc_app/main.py
#
# 当你使用第二种方式时，Python 默认只会把当前脚本所在目录 `pc_app/`
# 放到模块搜索路径里，而不会自动把项目根目录放进去。
#
# 这样一来，像 `from pc_app.main_window import MainWindow` 这种“从包根开始的绝对导入”
# 就可能失败，因为解释器看不到上层目录。
#
# 因此这里做一个非常常见的启动兼容处理：
# 如果当前文件不是作为包模块运行，就手动把项目根目录插入 `sys.path`。
if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from pc_app.main_window import MainWindow


def main() -> int:
    """创建 Qt 应用并启动主窗口。

    函数作用：
        这是桌面端程序的总入口函数，负责初始化图形界面环境并显示主窗口。

    调用时机：
        当文件被直接执行，或者以 `python -m pc_app.main` 方式启动时调用。

    参数含义：
        无。

    返回值含义：
        返回 Qt 事件循环结束时的退出码（整数）。
        正常退出通常返回 0。

    是否会修改全局状态：
        会初始化 Qt 全局应用对象，并配置 pyqtgraph 的全局绘图外观。

    与其他模块/任务/回调的关系：
        - 创建 `QApplication` 后，整个 Qt GUI 系统才真正开始可用
        - 创建 `MainWindow` 后 ，BLE、协议、绘图、日志等模块才会被逐步实例化
        - 最终通过 `app.exec()` 进入事件循环，后续所有按钮点击、窗口重绘、信号回调
          都依赖这个事件循环驱动
    """
    # pyqtgraph 是当前项目使用的实时绘图库。
    # `setConfigOptions()` 用来设置它的全局显示参数。
    #
    # 这里这样配置的原因：
    # - antialias=False：关闭抗锯齿，减少高频实时绘图的额外开销
    # - background="w"：背景设为白色
    # - foreground="k"：前景（文字、坐标轴等）设为黑色
    #
    # 这是一种偏“示波器/工程工具”风格的默认外观，清晰、直接、性能也更稳妥。
    pg.setConfigOptions(antialias=False, background="w", foreground="k")

    # QApplication 是 Qt GUI 程序的核心对象。
    #
    # 你可以把它理解成：
    # “整个桌面应用的运行环境管理器”。
    # 它负责：
    # - 管理窗口系统交互
    # - 管理事件循环
    # - 分发用户输入（点击、键盘、重绘等）
    #
    # 没有它，任何 QWidget、QMainWindow、信号槽交互都无法正常工作。
    app = QApplication(sys.argv)

    # 设置应用名称。
    # 这个名称可能出现在任务栏、窗口管理器或某些系统调试信息中。
    app.setApplicationName("ESP32-C3 BLE ADC Scope")

    # 创建主窗口对象。
    # MainWindow 内部会继续创建：
    # - BLE 桥接器
    # - 数据记录器
    # - 实时绘图控件
    # - 各种按钮、标签、日志区
    window = MainWindow()

    # show() 的作用是把窗口真正显示到桌面上。
    # 只创建对象而不调用 show()，窗口不会出现。
    window.show()

    # `app.exec()` 会启动 Qt 的事件循环。
    #
    # 事件循环是 GUI 程序的核心机制：
    # - 用户点击按钮时，它负责分发点击事件
    # - 定时器超时时，它负责触发回调
    # - 窗口需要重绘时，它负责调度刷新
    # - 信号槽连接后的异步更新，也依赖它驱动
    #
    # 这行通常会一直阻塞，直到用户关闭程序。
    return app.exec()


if __name__ == "__main__":
    # 这里采用 Python 程序中很常见的写法：
    # 用 `SystemExit` 把 main() 的返回值转换成进程退出码。
    #
    # 这对初学者理解“脚本退出”和“程序退出状态”很有帮助：
    # main() 返回一个数字，而 `raise SystemExit(...)` 会把这个数字传给操作系统。
    raise SystemExit(main())
