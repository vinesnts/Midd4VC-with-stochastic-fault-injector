
from . import math_jobs
from . import image_jobs

JOBS_CATALOG = {
    "math.factorial": math_jobs.factorial,
    "math.add": math_jobs.add,
    "math.multiply": math_jobs.multiply,
    "math.fibonacci": math_jobs.fibonacci,
    "image.classification": image_jobs.classification,
    # Add more functions as needed.
}
