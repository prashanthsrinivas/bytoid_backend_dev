import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from services.redis_service import get_redis

redis_service = get_redis()

# worker pool
executor = ThreadPoolExecutor(max_workers=4)


class JobManager:

    @staticmethod
    async def submit_job(func, *args, **kwargs):

        job_id = str(uuid.uuid4())

        await redis_service.set(f"job:{job_id}", {"status": "queued"}, ex=3600)

        loop = asyncio.get_running_loop()

        loop.run_in_executor(
            executor,
            lambda: asyncio.run(JobManager._run_job(job_id, func, *args, **kwargs)),
        )

        return job_id

    @staticmethod
    async def _run_job(job_id, func, *args, **kwargs):

        try:
            await redis_service.set(f"job:{job_id}", {"status": "processing"}, ex=3600)

            # ✅ inject job_id
            kwargs["job_id"] = job_id

            result = await func(*args, **kwargs)

            await redis_service.set(
                f"job:{job_id}", {"status": "completed", "data": result}, ex=3600
            )

        except Exception as e:
            await redis_service.set(
                f"job:{job_id}", {"status": "failed", "error": str(e)}, ex=3600
            )
