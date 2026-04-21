"""
PROBE 1: Debug Task
A user reports: "My pagination is broken — when I have 25 items and go to page 3,
it says 'Page out of range' but there should be 5 items on page 3."

Find and fix all bugs in this function.
"""

import math

def paginate_results(items, page, per_page=10):
    """Return a page of results with metadata."""
    total = len(items)
    total_pages = total // per_page

    if page < 1 or page > total_pages:
        return {"error": "Page out of range", "total_pages": total_pages}

    start = (page - 1) * per_page
    end = start + per_page

    return {
        "items": items[start:end],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1
    }


# Test cases the user ran:
# >>> paginate_results(list(range(25)), 3)
# {'error': 'Page out of range', 'total_pages': 2}
#
# >>> paginate_results(list(range(25)), 2)
# {'items': [10, 11, 12, 13, 14, 15, 16, 17, 18, 19], 'page': 2, ...}
#
# >>> paginate_results([], 1)
# {'error': 'Page out of range', 'total_pages': 0}
