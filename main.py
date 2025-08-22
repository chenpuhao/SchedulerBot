import asyncio
import aiohttp
import json
import os
import re
from datetime import datetime, timedelta
from pkg.plugin.context import register, handler, BasePlugin, APIHost, EventContext
from pkg.plugin.events import GroupNormalMessageReceived, PersonNormalMessageReceived
import pkg.platform.types as platform_types

DATA_FILE = "scheduler_jobs.json"


@register(name="SchedulerBot", description="群/私聊定时消息插件(支持持久化)", version="0.1.0", author="wangling")
class SchedulerBotPlugin(BasePlugin):

    def __init__(self, host: APIHost):
        super().__init__(host)
        self.tasks = []
        self.jobs = {"group": {}, "person": {}}
        self.adapters = []

    async def initialize(self):
        """插件启动时加载持久化任务"""
        try:
            # 获取消息平台适配器
            self.adapters = self.host.get_platform_adapters()
            self.ap.logger.info(f"SchedulerBot 找到 {len(self.adapters)} 个消息平台适配器")

            await self.load_jobs()

            # 恢复所有任务
            for gid, job_list in self.jobs.get("group", {}).items():
                for job in job_list:
                    task = asyncio.create_task(
                        self.schedule_job(int(gid), job["url"], job["hour"], job["minute"], is_group=True)
                    )
                    job["task"] = task
                    self.tasks.append(task)

            for uid, job_list in self.jobs.get("person", {}).items():
                for job in job_list:
                    task = asyncio.create_task(
                        self.schedule_job(int(uid), job["url"], job["hour"], job["minute"], is_group=False)
                    )
                    job["task"] = task
                    self.tasks.append(task)

            self.ap.logger.info(f"SchedulerBot 已恢复 {len(self.tasks)} 个持久化任务")
        except Exception as e:
            self.ap.logger.error(f"SchedulerBot 初始化失败: {e}")

    async def fetch_content(self, url: str) -> str:
        """异步获取网页内容"""
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        return text.replace("\\n", "\n")
                    else:
                        return f"[请求失败] HTTP {resp.status}"
        except asyncio.TimeoutError:
            return "[请求失败] 超时"
        except aiohttp.ClientError as e:
            return f"[请求失败] 网络错误: {e}"
        except Exception as e:
            return f"[请求失败] 未知错误: {e}"

    async def schedule_job(self, target_id: int, url: str, hour: int, minute: int, is_group=True):
        """循环执行每天定时任务"""
        kind = "群" if is_group else "私聊"
        self.ap.logger.info(f"启动定时任务 [{kind}={target_id}] {hour:02d}:{minute:02d} -> {url}")

        try:
            while True:
                now = datetime.now()
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # 如果已过今天的时间，等到明天
                if target <= now:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                self.ap.logger.info(f"任务 [{kind}={target_id}] 将在 {wait_seconds:.1f} 秒后执行 (目标时间: {target})")

                await asyncio.sleep(wait_seconds)

                # 时间到了执行任务
                self.ap.logger.info(f"开始执行任务 [{kind}={target_id}] {hour:02d}:{minute:02d}")
                content = await self.fetch_content(url)

                try:
                    # 使用正确的LangBot API发送消息
                    if self.adapters:
                        adapter = self.adapters[0]  # 使用第一个适配器
                        target_type = "group" if is_group else "person"

                        # 创建消息链
                        message_chain = platform_types.MessageChain([
                            platform_types.Plain(content)
                        ])

                        await self.host.send_active_message(
                            adapter=adapter,
                            target_type=target_type,
                            target_id=str(target_id),
                            message=message_chain
                        )

                        self.ap.logger.info(f"任务执行完成 [{kind}={target_id}] 内容长度: {len(content)}")
                    else:
                        self.ap.logger.error("没有可用的消息平台适配器")

                except Exception as e:
                    self.ap.logger.error(f"发送消息失败 [{kind}={target_id}]: {e}")
                    import traceback
                    self.ap.logger.error(traceback.format_exc())

        except asyncio.CancelledError:
            self.ap.logger.info(f"定时任务已取消 [{kind}={target_id}] {hour:02d}:{minute:02d}")
            raise
        except Exception as e:
            self.ap.logger.error(f"定时任务异常 [{kind}={target_id}]: {e}")
            import traceback
            self.ap.logger.error(traceback.format_exc())

    async def save_jobs(self):
        """保存任务到文件"""
        try:
            data = {"group": {}, "person": {}}
            for gid, jobs in self.jobs.get("group", {}).items():
                data["group"][str(gid)] = [
                    {"url": j["url"], "hour": j["hour"], "minute": j["minute"]}
                    for j in jobs
                ]
            for uid, jobs in self.jobs.get("person", {}).items():
                data["person"][str(uid)] = [
                    {"url": j["url"], "hour": j["hour"], "minute": j["minute"]}
                    for j in jobs
                ]

            temp_file = DATA_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, DATA_FILE)

        except Exception as e:
            self.ap.logger.error(f"保存任务失败: {e}")

    async def load_jobs(self):
        """从文件中恢复任务"""
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "group" in data and "person" in data:
                    self.jobs = data
                else:
                    self.jobs = {"group": {}, "person": {}}
            else:
                self.jobs = {"group": {}, "person": {}}
        except Exception as e:
            self.ap.logger.error(f"加载任务失败: {e}")
            self.jobs = {"group": {}, "person": {}}

    def parse_set_command(self, msg: str):
        """解析 /set {url} {HH/MM} 格式的命令"""
        try:
            pattern = r'/set\s+\{([^}]+)\}\s+\{(\d{1,2}/\d{1,2})\}'
            match = re.match(pattern, msg.strip())
            if match:
                url = match.group(1).strip()
                time_str = match.group(2)

                if not (url.startswith('http://') or url.startswith('https://')):
                    return None, "URL必须以http://或https://开头"

                try:
                    hour, minute = map(int, time_str.split("/"))
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        return None, "时间格式错误，小时应为00-23，分钟应为00-59"
                    return (url, hour, minute), None
                except ValueError:
                    return None, "时间格式错误"
            return None, "命令格式错误"
        except Exception as e:
            return None, f"解析命令失败: {e}"

    async def handle_command(self, ctx: EventContext, is_group: bool):
        try:
            msg = ctx.event.text_message.strip()
            target_id = ctx.event.group_id if is_group else ctx.event.sender_id
            jobs_dict = self.jobs["group"] if is_group else self.jobs["person"]

            # 帮助命令
            if msg == "/help":
                help_text = (
                    "定时任务插件使用方法:\n"
                    "/set {url} {HH/MM} - 添加定时任务\n"
                    "/list - 查看任务\n"
                    "/del {index} - 删除指定任务\n"
                    "/help - 显示帮助\n"
                    "/test {url} - 测试URL请求\n"
                    "/status - 查看插件状态\n"
                    "示例: /set {http://example.com/msg.txt} {08/30}"
                )
                ctx.add_return("reply", [help_text])
                ctx.prevent_default()
                return

            # 状态查看命令
            if msg == "/status":
                status_text = (
                    f"插件状态:\n"
                    f"运行任务数: {len([t for t in self.tasks if not t.done()])}\n"
                    f"总任务数: {len(self.tasks)}\n"
                    f"消息适配器数: {len(self.adapters)}\n"
                    f"适配器类型: {[type(a).__name__ for a in self.adapters]}"
                )
                ctx.add_return("reply", [status_text])
                ctx.prevent_default()
                return

            # 测试命令
            if msg.startswith("/test {") and msg.endswith("}"):
                pattern = r'/test\s+\{([^}]+)\}'
                match = re.match(pattern, msg.strip())
                if match:
                    url = match.group(1).strip()
                    content = await self.fetch_content(url)
                    ctx.add_return("reply", [f"测试结果:\n{content[:500]}{'...' if len(content) > 500 else ''}"])
                else:
                    ctx.add_return("reply", ["格式错误，请使用: /test {url}"])
                ctx.prevent_default()
                return

            # 设置任务
            if msg.startswith("/set {"):
                result, error = self.parse_set_command(msg)
                if result is None:
                    ctx.add_return("reply", [f"格式错误: {error}\n正确格式: /set {{url}} {{HH/MM}}"])
                    ctx.prevent_default()
                    return

                url, hour, minute = result

                existing_jobs = jobs_dict.get(str(target_id), [])
                for job in existing_jobs:
                    if job["url"] == url and job["hour"] == hour and job["minute"] == minute:
                        ctx.add_return("reply", ["相同的任务已存在"])
                        ctx.prevent_default()
                        return

                # 创建并启动任务
                task = asyncio.create_task(
                    self.schedule_job(target_id, url, hour, minute, is_group=is_group)
                )
                job = {"url": url, "hour": hour, "minute": minute, "task": task}
                jobs_dict.setdefault(str(target_id), []).append(job)
                self.tasks.append(task)
                await self.save_jobs()

                # 计算下次执行时间
                now = datetime.now()
                next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(days=1)

                ctx.add_return("reply",
                               [f"已设置任务: 每天 {hour:02d}:{minute:02d} 请求 {url}\n下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S')}\n任务ID: {id(task)}"])
                ctx.prevent_default()
                return

            # 查看任务
            if msg == "/list":
                jobs = jobs_dict.get(str(target_id), [])
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if not jobs:
                    ctx.add_return("reply", [f"暂无定时任务\n当前时间: {current_time}"])
                else:
                    text = "当前定时任务:\n"
                    for i, job in enumerate(jobs, 1):
                        # 计算下次执行时间
                        now = datetime.now()
                        next_run = now.replace(hour=job['hour'], minute=job['minute'], second=0, microsecond=0)
                        if next_run <= now:
                            next_run += timedelta(days=1)

                        task_status = "运行中" if "task" in job and not job["task"].done() else "已停止"
                        text += f"{i}. 每天 {job['hour']:02d}:{job['minute']:02d} - {job['url']}\n"
                        text += f"   下次执行: {next_run.strftime('%m-%d %H:%M')} | 状态: {task_status}\n"
                    text += f"\n当前时间: {current_time}"
                    ctx.add_return("reply", [text])
                ctx.prevent_default()
                return

            # 删除任务
            if msg.startswith("/del {") and msg.endswith("}"):
                pattern = r'/del\s+\{(\d+)\}'
                match = re.match(pattern, msg.strip())
                if match:
                    try:
                        index = int(match.group(1)) - 1
                        jobs = jobs_dict.get(str(target_id), [])
                        if 0 <= index < len(jobs):
                            job = jobs.pop(index)
                            if "task" in job and job["task"]:
                                job["task"].cancel()
                            await self.save_jobs()
                            ctx.add_return("reply", [f"已删除任务: {job['hour']:02d}:{job['minute']:02d} {job['url']}"])
                        else:
                            ctx.add_return("reply", ["任务编号不存在"])
                    except (ValueError, IndexError):
                        ctx.add_return("reply", ["无效编号"])
                else:
                    ctx.add_return("reply", ["用法: /del {任务编号}"])
                ctx.prevent_default()
                return

        except Exception as e:
            self.ap.logger.error(f"处理命令失败: {e}")
            import traceback
            self.ap.logger.error(traceback.format_exc())
            ctx.add_return("reply", ["处理命令时发生错误"])
            ctx.prevent_default()

    @handler(GroupNormalMessageReceived)
    async def group_msg_handler(self, ctx: EventContext):
        await self.handle_command(ctx, is_group=True)

    @handler(PersonNormalMessageReceived)
    async def person_msg_handler(self, ctx: EventContext):
        await self.handle_command(ctx, is_group=False)

    def __del__(self):
        try:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            self.ap.logger.info("SchedulerBot 插件卸载，定时任务已停止")
        except:
            pass
