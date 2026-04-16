"""BLE 通信桥接模块。

这个文件是桌面端上位机里最“中间层”的一个模块：
它既不直接负责显示界面，也不直接负责画图，而是负责把 BLE 通信能力
稳定地桥接给 Qt 图形界面。

从架构角度看，它位于这样一条链路中：

    Qt 按钮/界面
        -> BleClientBridge 对外方法
        -> 后台 asyncio 事件循环线程
        -> bleak BLE API
        -> 收到 Notify 原始 bytes
        -> protocol.parse_frame() 解析
        -> Qt Signal 发回主线程
        -> 主窗口 / 绘图 / 状态栏 / 日志

为什么这里要单独做一个桥接层，而不是直接在主窗口里写 BLE 代码？
原因有三点：
1. Qt 主线程主要负责 GUI，尽量不要被 BLE I/O 阻塞
2. bleak 本质上是 asyncio 风格，更适合在单独事件循环里运行
3. 把通信层封装成 QObject + Signal，主窗口会更清晰、更容易维护

这个文件对初学者尤其值得学习的点包括：
- Qt 的信号槽机制如何跨线程回传结果
- asyncio 事件循环如何放到后台线程里运行
- bleak 的扫描、连接、服务发现、start_notify() 的基本使用方法
- 如何把“原始 bytes 数据流”升级成“结构化帧对象 + 状态统计”
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from pc_app.protocol import (
    CHARACTERISTIC_UUID_CANDIDATES,
    DEFAULT_DEVICE_NAME,
    FrameStats,
    ProtocolError,
    SERVICE_UUID_CANDIDATES,
    parse_frame,
    uuid_matches,
)


class BleClientBridge(QObject):
    """把 bleak 的异步 BLE 通信能力桥接给 Qt 界面层。

    类作用：
        这个类向上提供“扫描、连接、断开”这样的同步风格入口，
        向下则在后台线程里运行 asyncio + bleak，并通过 Qt Signal 把结果发回主线程。

    调用时机：
        由 `MainWindow` 在初始化阶段创建，并在整个上位机生命周期内持续存在。

    参数含义：
        parent：Qt 父对象。传入主窗口后，Qt 对象树会帮助管理其生命周期。

    返回值含义：
        这是类定义本身，不直接返回值；实例化后得到一个 BLE 通信桥接器。

    是否会修改全局状态：
        不修改 Python 进程级全局变量，但会维护自身内部连接状态、统计状态、设备缓存和后台线程。

    与其他模块/任务/回调的关系：
        - `main_window.py` 通过它发起扫描/连接/断开动作
        - `protocol.py` 为它提供协议解析函数和数据结构
        - Qt 主线程通过 Signal 接收它发出的扫描结果、收包数据和错误信息

    设计说明：
        之所以继承 QObject，是因为 Qt 的 Signal/Slot 机制天然适合 GUI 程序：
        后台线程处理完 BLE 数据后，只需要 emit 一个信号，Qt 就会帮我们把数据安全送回界面线程。
    """

    # ==================== Qt 对外信号定义 ====================
    # 这些 Signal 相当于“桥接层对 GUI 的输出接口”。

    # 扫描完成后，把设备列表发给界面层。
    devices_updated = Signal(list)

    # 连接状态变化信号：
    # 参数依次是 (是否已连接, 设备名, 设备地址)。
    connection_state_changed = Signal(bool, str, str)

    # 成功解析到一帧协议数据后发出。
    frame_received = Signal(object)

    # 运行统计信息更新信号。
    stats_updated = Signal(dict)

    # 普通日志与错误日志。
    log_message = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        """初始化 BLE 桥接器。

        函数作用：
            创建后台 asyncio 事件循环线程，并初始化各种运行时状态变量。

        调用时机：
            由主窗口在程序启动时创建一次。

        参数含义：
            parent：Qt 父对象，用于生命周期管理。

        返回值含义：
            无。

        是否会修改全局状态：
            不修改进程全局变量，但会启动一个后台线程并创建一个新的 asyncio 事件循环。

        与其他模块/任务/回调的关系：
            初始化完成后，主窗口就可以调用 `scan_devices()`、`connect_device()` 等方法。
        """
        super().__init__(parent)

        # 创建一个全新的 asyncio 事件循环。
        #
        # 为什么不直接复用主线程事件循环：
        # 因为当前项目主线程已经被 Qt 事件循环占用，
        # 如果硬把 asyncio 塞进去，会让整体结构复杂很多。
        self._loop = asyncio.new_event_loop()

        # 启动后台线程，并让它运行 _run_loop()。
        # daemon=True 表示这是一个守护线程；理论上主进程退出时它不会阻止解释器结束，
        # 但工程上我们仍然会主动调用 shutdown() 来优雅关闭它。
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # 当前活跃的 bleak 客户端对象。未连接时为 None。
        self._client: BleakClient | None = None

        # 扫描结果缓存。
        # 键是设备地址，值是包含名称、地址、RSSI 的字典。
        # 这样连接时可以根据地址快速找回设备名。
        self._device_cache: dict[str, dict[str, Any]] = {}

        # 当前实际用于 start_notify() 的特征 UUID。
        self._notify_uuid: str | None = None

        # 当前已连接设备的信息，主要给状态栏和日志显示使用。
        self._connected_name = ""
        self._connected_address = ""

        # 统计信息对象。
        # 包括有效帧、无效帧、帧率、最近错误等，最终会发给界面层显示。
        self._stats = FrameStats()

        # 下面两个变量用来估算帧率：
        # - _fps_window_start 记录本轮统计窗口开始时间
        # - _fps_window_frames 记录窗口内累计收到多少帧
        self._fps_window_start = time.perf_counter()
        self._fps_window_frames = 0

    def _run_loop(self) -> None:
        """在线程中启动 asyncio 事件循环。

        函数作用：
            把刚创建好的事件循环绑定到当前后台线程，并持续运行它。

        调用时机：
            仅在线程启动时由 `threading.Thread(target=...)` 自动调用一次。

        返回值含义：
            无。这个函数通常不会主动返回，除非事件循环被 stop。
        """
        # 把 self._loop 声明为“当前线程的活动事件循环”。
        asyncio.set_event_loop(self._loop)

        # run_forever() 会一直运行，直到其他线程通过 call_soon_threadsafe(loop.stop)
        # 请求停止这个事件循环。
        self._loop.run_forever()

    def shutdown(self) -> None:
        """优雅关闭 BLE 连接和后台事件循环线程。

        函数作用：
            在程序退出前，尽量按正确顺序做收尾：
            1. 先异步断开 BLE 连接
            2. 再停止 asyncio 事件循环
            3. 最后等待后台线程结束

        调用时机：
            由主窗口在 closeEvent() 中调用。

        设计说明：
            GUI 程序里“安全退出”很重要。
            如果不主动关闭后台 BLE 线程，虽然进程退出时也可能被系统回收，
            但那不是一个教学上推荐的写法。
        """
        # asyncio.run_coroutine_threadsafe() 的作用：
        # 把一个协程扔到指定事件循环里执行，并立即返回一个 concurrent future。
        future = asyncio.run_coroutine_threadsafe(self._disconnect_internal(), self._loop)

        # shutdown 阶段优先保证“不因为异常卡死退出流程”。
        with contextlib.suppress(Exception):
            future.result(timeout=5)

        # call_soon_threadsafe() 允许其他线程安全地给该事件循环投递一个“停止”动作。
        self._loop.call_soon_threadsafe(self._loop.stop)

        # join() 等待后台线程退出。
        self._thread.join(timeout=2)

    def scan_devices(self) -> None:
        """向界面层暴露的“开始扫描 BLE 设备”入口。"""
        self._submit(self._scan_devices())

    def connect_device(self, address: str) -> None:
        """向界面层暴露的“连接指定地址设备”入口。"""
        self._submit(self._connect_device(address))

    def disconnect_device(self) -> None:
        """向界面层暴露的“断开当前连接”入口。"""
        self._submit(self._disconnect_internal())

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> None:
        """把协程安全提交到后台 asyncio 事件循环执行。

        函数作用：
            统一处理“主线程想让后台线程执行某个异步操作”这个场景。

        参数含义：
            coro：要执行的协程对象，例如 `_scan_devices()` 或 `_connect_device()`。

        设计说明：
            之所以单独封装一层，而不是每个公有方法都手写一次，
            是为了统一异常处理路径。
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._handle_future_result)

    def _handle_future_result(self, future: Any) -> None:
        """统一处理后台协程执行结果。

        作用：
            把后台异步任务里抛出的异常集中转换成 GUI 可见的错误信息，
            避免异常静默丢失。
        """
        with contextlib.suppress(asyncio.CancelledError):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                self._report_error(str(exc))

    async def _scan_devices(self) -> None:
        """实际执行 BLE 设备扫描。

        函数作用：
            调用 bleak 的扫描接口，收集附近可见 BLE 设备，并整理成 GUI 容易使用的列表。

        调用时机：
            由 `scan_devices()` 提交到后台事件循环中执行。

        与其他模块的关系：
            扫描结果最终会通过 `devices_updated` 信号发给主窗口。
        """
        self.log_message.emit("Scanning BLE devices...")

        # BleakScanner.discover(timeout=...) 会在指定秒数内执行一次扫描，
        # 返回扫描到的设备对象列表。
        devices = await BleakScanner.discover(timeout=5.0)
        items: list[dict[str, Any]] = []

        for device in devices:
            # bleak 返回的设备对象里通常会包含 name/address/rssi 等信息。
            # 这里我们把它们转成普通 dict，便于后续界面层直接消费。
            record = {
                "name": device.name or "<Unknown>",
                "address": device.address,
                "rssi": getattr(device, "rssi", None),
            }

            # 同时按地址缓存起来，便于稍后连接时从地址反查显示名。
            self._device_cache[device.address] = record
            items.append(record)

        # 排序策略：
        # 1. 名称等于默认目标设备名的优先
        # 2. 然后按名称排序
        # 3. 最后按地址排序
        #
        # 这样在教学和实际使用中都更方便：
        # 目标设备通常更靠前，减少误点和搜索成本。
        items.sort(key=lambda item: (item["name"] != DEFAULT_DEVICE_NAME, item["name"], item["address"]))
        self.devices_updated.emit(items)
        self.log_message.emit(f"Scan complete, found {len(items)} device(s).")

    async def _connect_device(self, address: str) -> None:
        """实际执行连接与 Notify 订阅流程。

        函数作用：
            完成以下一整条链路：
            1. 如有旧连接，先断开
            2. 创建 BleakClient
            3. 建立 BLE 连接
            4. 解析服务表，找到 Notify 特征
            5. 调用 start_notify() 开启持续收包
            6. 更新本地连接状态并通知界面

        参数含义：
            address：目标设备蓝牙地址。
        """
        # 如果已经连接的就是同一个地址，就没有必要重复连接。
        if self._client and self._client.is_connected and address == self._connected_address:
            self.log_message.emit("Already connected to the selected device.")
            return

        # 连接新设备前，先确保旧连接被释放干净。
        await self._disconnect_internal()

        device_name = self._device_cache.get(address, {}).get("name", "")
        self.log_message.emit(f"Connecting to {address}...")

        # BleakClient 是 bleak 的核心客户端对象。
        #
        # disconnected_callback 的作用：
        # 如果对端主动断开，或者系统层面连接消失，bleak 会回调这个函数，
        # 我们就能及时把 GUI 状态同步成“未连接”。
        client = BleakClient(address, disconnected_callback=self._handle_disconnect)
        try:
            # connect() 执行真正的连接动作。
            await client.connect()

            self.log_message.emit("BLE link established, resolving notify characteristic...")

            # 找到真正的 Notify 特征 UUID。
            notify_uuid = await self._resolve_notify_characteristic(client)

            # start_notify() 的意义是：
            # 一旦设备侧对该特征发送 Notify，bleak 就会自动回调 _notification_handler。
            await client.start_notify(notify_uuid, self._notification_handler)
        except Exception:
            # 如果连接流程中途失败，尽量把已经建立一半的连接清理掉，
            # 避免出现“界面看起来没连上，但底层资源没释放干净”的情况。
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise

        # 到这里说明整个连接 + Notify 开启流程已经成功。
        self._client = client
        self._connected_address = address
        self._connected_name = device_name
        self._notify_uuid = notify_uuid

        # 新连接建立时，把上一轮运行统计全部清空，避免把旧连接的数据混进来。
        self._reset_stats()
        self.connection_state_changed.emit(True, self._connected_name, self._connected_address)
        self._emit_stats(force=True)
        self.log_message.emit(
            f"Connected to {self._connected_name or '<Unknown>'} ({self._connected_address}), notify={self._notify_uuid}"
        )

    async def _disconnect_internal(self) -> None:
        """内部断开函数。

        函数作用：
            统一处理所有“需要断开 BLE 连接”的场景。

        调用时机：
            - 用户主动点击断开
            - 连接新设备前清理旧连接
            - 程序退出前安全收尾

        设计说明：
            把断开逻辑集中到一个函数里，能避免不同场景下出现“有的地方停 notify，有的地方没停”的问题。
        """
        if not self._client:
            return

        client = self._client
        notify_uuid = self._notify_uuid

        # 先把对象级引用清掉，避免其他路径继续误以为连接仍然可用。
        self._client = None
        self._notify_uuid = None

        if client.is_connected:
            # 先 stop_notify()，再 disconnect()，顺序更清晰，也更接近语义。
            with contextlib.suppress(Exception):
                if notify_uuid:
                    await client.stop_notify(notify_uuid)
            with contextlib.suppress(Exception):
                await client.disconnect()

        self._finish_disconnect("Disconnected.")

    async def _resolve_notify_characteristic(self, client: BleakClient) -> str:
        """从服务表中找到用于接收数据的 Notify 特征 UUID。

        函数作用：
            在 BLE 连接已经建立后，从设备暴露的服务/特征中找出最合适的目标 Notify 特征。

        匹配优先级：
            1. 精确匹配预期 Characteristic UUID
            2. 如果 Service UUID 匹配，就取该服务下的 notify 特征
            3. 仍找不到时，退化到第一个带 notify 属性的特征

        设计说明：
            这种“逐层降级”的查找方式兼顾了严谨性和兼容性：
            平时优先用精确 UUID，联调时又不至于因为细微差异完全找不到特征。
        """
        services = self._get_service_collection(client)
        fallback_notify: str | None = None

        for service in services:
            service_match = uuid_matches(service.uuid, SERVICE_UUID_CANDIDATES)
            for char in service.characteristics:
                # 只关心带 notify 属性的特征。
                if "notify" not in char.properties:
                    continue

                # 先记住第一个 notify 特征，作为最终兜底方案。
                if fallback_notify is None:
                    fallback_notify = str(char.uuid)

                # 优先精确匹配目标 Characteristic UUID。
                if uuid_matches(str(char.uuid), CHARACTERISTIC_UUID_CANDIDATES):
                    return str(char.uuid)

                # 如果 Service UUID 是目标服务，则该服务下的 notify 特征也可接受。
                if service_match:
                    return str(char.uuid)

        if fallback_notify:
            self.log_message.emit("Notify characteristic matched by properties fallback.")
            return fallback_notify

        raise BleakError("No notify characteristic found on the target device.")

    def _get_service_collection(self, client: BleakClient):
        """获取 bleak 客户端当前已发现的服务集合。

        函数作用：
            把 bleak 不同版本在服务发现接口上的差异收敛到一个地方处理。

        设计说明：
            之前我们已经遇到过 bleak API 版本差异导致的报错。
            因此把服务获取逻辑单独封装出来，是一种很有工程价值的写法：
            以后升级 bleak 时，兼容处理只需要改这里。
        """
        # bleak 2.x 通过 `client.services` 暴露服务集合。
        services = getattr(client, "services", None)
        if services is not None:
            return services

        # 如果发现是老版本风格接口，这里明确抛出一个更容易理解的错误。
        get_services = getattr(client, "get_services", None)
        if callable(get_services):
            raise BleakError("Legacy bleak get_services() API is not supported by this app version.")

        raise BleakError("Service discovery is unavailable on the current BleakClient instance.")

    def _notification_handler(self, _sender: Any, data: bytearray) -> None:
        """Notify 数据回调函数。

        函数作用：
            每次设备侧发来一帧 Notify 数据时：
            1. 把 bytearray 转成 bytes
            2. 调协议解析器 parse_frame()
            3. 更新运行统计
            4. 把解析成功的帧对象发给 GUI

        调用时机：
            由 bleak 在 start_notify() 成功后自动回调。

        参数含义：
            _sender：发送该 Notify 的特征对象或句柄，当前项目暂不使用。
            data：收到的原始二进制负载。
        """
        try:
            frame = parse_frame(bytes(data))
        except ProtocolError as exc:
            # 非法帧不让程序崩溃，而是：
            # 1. 计入 invalid_frames
            # 2. 记录错误文本
            # 3. 把错误同步给 GUI
            self._stats.invalid_frames += 1
            self._stats.last_error = str(exc)
            self._emit_stats(force=True)
            self.error_occurred.emit(str(exc))
            return

        # 成功解析后，更新“最近一帧”的核心统计信息。
        self._stats.valid_frames += 1
        self._stats.last_frame_id = frame.frame_id
        self._stats.last_timestamp_ms = frame.timestamp_ms

        # 这一帧进入当前 FPS 统计窗口。
        self._fps_window_frames += 1
        self._refresh_fps()
        self._emit_stats(force=True)

        # 把解析好的结构化帧对象发给界面层。
        self.frame_received.emit(frame)

    def _refresh_fps(self) -> None:
        """按滑动时间窗口估算当前帧率。

        设计说明：
            这里没有为每一帧都做复杂时间统计，而是用一个约 1 秒窗口做简单估算。
            好处是实现简单、开销低，而且对实时状态显示已经足够。
        """
        now = time.perf_counter()
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            self._stats.frame_rate = self._fps_window_frames / elapsed
            self._fps_window_frames = 0
            self._fps_window_start = now

    def _reset_stats(self) -> None:
        """在新连接开始时重置统计信息。"""
        self._stats = FrameStats()
        self._fps_window_start = time.perf_counter()
        self._fps_window_frames = 0

    def _emit_stats(self, force: bool = False) -> None:
        """把当前统计信息整理成字典并发给界面层。

        参数含义：
            force：是否强制立即推送统计。
            当前实现里即使 force=False，函数也会在必要时先刷新 FPS 后再发送。
        """
        if not force:
            self._refresh_fps()

        # 用普通 dict 而不是直接发 FrameStats，
        # 是因为界面层按键值读取时会更直观，也更方便以后渐进调整字段。
        payload = {
            "connected": bool(self._client and self._client.is_connected),
            "device_name": self._connected_name,
            "device_address": self._connected_address,
            "valid_frames": self._stats.valid_frames,
            "invalid_frames": self._stats.invalid_frames,
            "frame_rate": self._stats.frame_rate,
            "last_frame_id": self._stats.last_frame_id,
            "last_timestamp_ms": self._stats.last_timestamp_ms,
            "last_error": self._stats.last_error,
        }
        self.stats_updated.emit(payload)

    def _report_error(self, message: str) -> None:
        """统一上报错误到统计区、错误区和日志区。

        设计意义：
            把错误出口集中起来，能保证：
            - 最近错误标签会更新
            - stats_updated 会同步错误文本
            - 日志区也会留下记录
        """
        self._stats.last_error = message
        self._emit_stats(force=True)
        self.error_occurred.emit(message)
        self.log_message.emit(f"ERROR: {message}")

    def _handle_disconnect(self, _client: BleakClient) -> None:
        """处理对端主动断开连接的回调。

        这里与 `_disconnect_internal()` 的区别在于：
        - `_disconnect_internal()` 是我们主动要求断开
        - `_handle_disconnect()` 是底层库告诉我们“连接已经被动消失了”

        两者最终都要做同一件事：把本地状态恢复成“未连接”。
        """
        self._finish_disconnect("BLE device disconnected by the remote side.")

    def _finish_disconnect(self, log_message: str) -> None:
        """统一完成断连后的本地状态清理。"""
        self._client = None
        self._notify_uuid = None
        self._connected_name = ""
        self._connected_address = ""
        self._reset_stats()
        self.connection_state_changed.emit(False, "", "")
        self._emit_stats(force=True)
        self.log_message.emit(log_message)
