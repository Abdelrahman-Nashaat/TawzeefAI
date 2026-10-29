"""Small synthetic development benchmark, not a production quality claim.

Run from backend: python -m app.semantic_evaluation ../.tooling/multilingual-e5-small
"""

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from app.db import connect, initialize
from app.knowledge_documents import initialize_knowledge_documents
from app.semantic_experiment import LocalE5, search_hybrid

DOCUMENTS = [
    ('leave-en', 'Employees receive 21 days of annual leave each year.'),
    ('leave-ar', 'الإجازة السنوية للموظفين هي ٢١ يوم عمل كل عام.'),
    ('interview', 'Interviews are held on Mondays at the office.'),
    ('remote', 'Remote employees may work from home two days per week.'),
    ('probation', 'The probation period for new employees is three months.'),
    ('expenses', 'Travel expenses are reimbursed only after receipts are submitted.'),
]
QUERIES = [
    ('How many days of annual leave do employees get?', 'leave-en'),
    ('ما عدد أيام الإجازة السنوية للموظفين؟', 'leave-ar'),
    ('When are interviews held?', 'interview'),
    ('How often can staff work from home?', 'remote'),
    ('كم مدة فترة التجربة للموظف الجديد؟', 'probation'),
    ('How can I be reimbursed for travel expenses?', 'expenses'),
    ('What is the company parental leave policy?', None),
    ('ما سياسة المكافآت السنوية؟', None),
    ('How much is the monthly health insurance premium?', None),
]


def peak_memory_mb():
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD),
                    ('peak', ctypes.c_size_t), ('current', ctypes.c_size_t),
                    ('quota_peak_paged', ctypes.c_size_t), ('quota_paged', ctypes.c_size_t),
                    ('quota_peak_nonpaged', ctypes.c_size_t), ('quota_nonpaged', ctypes.c_size_t),
                    ('pagefile', ctypes.c_size_t), ('peak_pagefile', ctypes.c_size_t)]
    data = Counters()
    data.cb = ctypes.sizeof(data)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = wintypes.HANDLE
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    get_memory.restype = wintypes.BOOL
    if not get_memory(get_process(), ctypes.byref(data), data.cb):
        return None
    return round(data.peak / 1024 / 1024, 1)


def run(model_dir: str):
    start = time.perf_counter()
    model = LocalE5(model_dir)
    init_seconds = time.perf_counter() - start
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'synthetic.sqlite3'
        initialize(path)
        initialize_knowledge_documents(path)
        with connect(path) as db:
            hr = db.execute("INSERT INTO users (username,display_name,role,password_hash) VALUES ('hr','HR','hr','x')").lastrowid
            for title, text in DOCUMENTS:
                db.execute('INSERT INTO knowledge_documents (title,audience,text,created_by) VALUES (?, ?, ?, ?)',
                           (title, 'public', text, hr))
        runs = []
        latencies = []
        for threshold in (0.75, 0.80, 0.85):
            results = []
            for question, expected in QUERIES:
                start = time.perf_counter()
                response = search_hybrid(path, 'candidate', question, model, min_similarity=threshold)
                latencies.append(time.perf_counter() - start)
                titles = [source['title'] for source in response['sources']]
                results.append({'query': question, 'expected': expected, 'retrieved': titles,
                                'max_similarity': round(max((s['similarity'] for s in response['sources']), default=0), 4)})
            positives = [item for item in results if item['expected'] is not None]
            negatives = [item for item in results if item['expected'] is None]
            runs.append({'threshold': threshold,
                         'positive_top1': sum(bool(i['retrieved']) and i['retrieved'][0] == i['expected'] for i in positives),
                         'positive_recall_at_3': sum(i['expected'] in i['retrieved'] for i in positives),
                         'positive_count': len(positives),
                         'negative_false_returns': sum(bool(i['retrieved']) for i in negatives),
                         'negative_count': len(negatives), 'cases': results})
    return {'scope': 'Tiny synthetic development set; no production quality claims.',
            'init_seconds': round(init_seconds, 3),
            'mean_query_seconds': round(statistics.mean(latencies), 3),
            'max_query_seconds': round(max(latencies), 3), 'peak_working_set_mb': peak_memory_mb(),
            'runs': runs}


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Provide a path to the pinned local model directory')
    print(json.dumps(run(sys.argv[1]), ensure_ascii=False, indent=2))
