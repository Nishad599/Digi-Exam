import asyncio
from sqlalchemy.future import select
from sqlalchemy import delete
from database import AsyncSessionLocal
import models

async def cleanup_duplicates():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(models.ExamEnrollment))
        enrollments = res.scalars().all()
        
        # Keep track of (student_id, exam_id) — keep the first, delete the rest
        seen = set()
        to_delete = []
        for e in enrollments:
            pair = (e.student_id, e.exam_id)
            if pair in seen:
                to_delete.append(e.id)
            else:
                seen.add(pair)
                
        if to_delete:
            print(f"Found {len(to_delete)} duplicate enrollments. Deleting them...")
            await db.execute(delete(models.ExamEnrollment).where(models.ExamEnrollment.id.in_(to_delete)))
            await db.commit()
            print("Successfully deleted duplicates.")
        else:
            print("No duplicate enrollments found.")

if __name__ == "__main__":
    asyncio.run(cleanup_duplicates())
