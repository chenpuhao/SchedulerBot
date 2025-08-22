import asyncio
import aiohttp
import json
import os
from datetime import datetime, timedelta
from pkg.plugin.context import register, handler, BasePlugin, APIHost, EventContext
from pkg.plugin.events import GroupNormalMessageReceived, PersonNormalMessageReceived

DATA_FILE = "scheduler_jobs.json"  # 存任务的文件


@register(name="SchedulerBot", description="群/私聊定时消息插件(支持持久化)", version="2.0", author="wangling")
class SchedulerBotPlugin(BasePlugin):

    def __init__(self, host: APIHost):
        self.tasks = []   # 存放 asyncio.Task 对象
        self.jobs = {"group": {}, "person": {}}
        # jobs = { "group": {group_id: [job...]}, "person": {user_id: [job...]} }
        self.loop = asyncio.get_event_loop()

    async def initialize(self):
        """插件启动时加载持久化任务"""
        self.ap.logger.info("SchedulerBot 插件初始化：尝试加载任务")
        self.load_jobs()
        # 恢复所有任务
        for gid, job_list in self.jobs.get("group", {}).items():
            for job in job_list:
                task = self.loop.create_task(
                    self.schedule_job(int(gid), job["url"], job["hour"], job["minute"], is_group=True)
                )
                job["task"] = task
                self.tasks.append(task)

        for uid, job_list in self.jobs.get("person", {}).items():
            for job in job_list:
                task = self.loop.create_task(
                    self.schedule_job(int(uid), job["url"], job["hour"], job["minute"], is_group=False)
                )
                job["task"] = task
                self.tasks.append(task)

        self.ap.logger.info("SchedulerBot 已恢复持久化任务")

    async def fetch_content(self, url: str) -> str:
        """异步获取网页内容"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    text = await resp.text()
                    return text.replace("\\n", "\n")
        except Exception as e:
            return f"[请求失败] {e}"

    async def schedule_job(self, target_id: int, url: str, hour: int, minute: int, is_group=True):
        """循环执行每天定时任务"""
        kind = "群" if is_group else "私聊"
        self.ap.logger.info(f"启动定时任务 [{kind}={target_id}] {hour:02d}:{minute:02d} -> {url}")

        while True:
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:  # 已过今天时间 -> 明天
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            content = await self.fetch_content(url)
            if is_group:
                await self.ap.sendGroupMessage(target_id, content)
            else:
                await self.ap.sendPersonMessage(target_id, content)

    def save_jobs(self):
        """保存任务到文件(不保存task对象)"""
        data = {"group": {}, "person": {}}
        # 群任务
        for gid, jobs in self.jobs.get("group", {}).items():
            data["group"][str(gid)] = [
                {"url": j["url"], "hour": j["hour"], "minute": j["minute"]}
                for j in jobs
            ]
        # 私人任务
        for uid, jobs in self.jobs.get("person", {}).items():
            data["person"][str(uid)] = [
                {"url": j["url"], "hour": j["hour"], "minute": j["minute"]}
                for j in jobs
            ]
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_jobs(self):
        """从文件中恢复任务"""
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.jobs = data
        else:
            self.jobs = {"group": {}, "person": {}}

    # ===== 公共命令处理逻辑 =====
    async def handle_command(self, ctx: EventContext, is_group: bool):
        msg = ctx.event.text_message.strip()
        target_id = ctx.event.group_id if is_group else ctx.event.sender_id
        jobs_dict = self.jobs["group"] if is_group else self.jobs["person"]

        # 帮助
        if msg.startswith("/set") and msg.strip() == "/set help":
            help_text = (
                "使用方法:\n"
                "/set {url} {HH/MM} - 添加定时任务\n"
                "/list - 查看任务\n"
                "/del {index} - 删除指定任务\n"
                "示例: /set http://example.com/msg.txt 08/30"
            )
            ctx.add_return("reply", [help_text])
            ctx.prevent_default()
            return

        # 设置任务
        if msg.startswith("/set "):
            parts = msg.split()
            if len(parts) == 3:
                url = parts[1]
                try:
                    hour, minute = map(int, parts.split("/"))
                except ValueError:
                    ctx.add_return("reply", ["时间格式错误，应为 HH/MM"])
                    ctx.prevent_default()
                    return

                task = self.loop.create_task(
                    self.schedule_job(target_id, url, hour, minute, is_group=is_group)
                )
                job = {"url": url, "hour": hour, "minute": minute, "task": task}
                jobs_dict.setdefault(str(target_id), []).append(job)
                self.tasks.append(task)
                self.save_jobs()

                ctx.add_return("reply", [f"已设置任务: 每天 {hour:02d}:{minute:02d} 请求 {url}"])
                ctx.prevent_default()
                return

        # 查看任务
        if msg == "/list":
            jobs = jobs_dict.get(str(target_id), [])
            if not jobs:
                ctx.add_return("reply", ["暂无定时任务"])
            else:
                text = "当前定时任务:\n"
                for i, job in enumerate(jobs, 1):
                    text += f"{i}. 每天 {job['hour']:02d}:{job['minute']:02d} - {job['url']}\n"
                ctx.add_return("reply", [text.strip()])
            ctx.prevent_default()
            return

        # 删除任务
        if msg.startswith("/del"):
            parts = msg.split()
            if len(parts) == 2:
                try:
                    index = int(parts[1]) - 1
                    jobs = jobs_dict.get(str(target_id), [])
                    if 0 <= index < len(jobs):
                        job = jobs.pop(index)
                        if "task" in job:
                            job["task"].cancel()
                        self.save_jobs()
                        ctx.add_return("reply", [f"已删除任务: {job['hour']:02d}:{job['minute']:02d} {job['url']}"])
                    else:
                        ctx.add_return("reply", ["任务编号不存在"])
                except ValueError:
                    ctx.add_return("reply", ["无效编号"])
            else:
                ctx.add_return("reply", ["用法: /del {任务编号}"])
            ctx.prevent_default()
            return

    # 群聊命令
    @handler(GroupNormalMessageReceived)
    async def group_msg_handler(self, ctx: EventContext):
        await self.handle_command(ctx, is_group=True)

    # 私聊命令
    @handler(PersonNormalMessageReceived)
    async def person_msg_handler(self, ctx: EventContext):
        await self.handle_command(ctx, is_group=False)

    def __del__(self):
        for task in self.tasks:
            task.cancel()
        self.ap.logger.info("SchedulerBot 插件卸载，定时任务已停止")
