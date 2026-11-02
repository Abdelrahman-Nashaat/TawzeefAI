"""Isolated local PDF rendering and Tesseract worker; no network or disk output."""
import io
import json
import os
import shutil
import subprocess
import sys

MAX_PAGES = 5
MAX_PIXELS = 16_000_000
MAX_SIDE = 5000
MAX_OUTPUT = 200_000


class MissingLanguage(Exception):
    pass


def _languages_available(path, language):
    output = subprocess.run([path, '--list-langs'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=10, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    return {item.encode('ascii') for item in language.split('+')}.issubset(
        {line.strip() for line in output.splitlines()})


def contain_process_tree():
    """Ensure Windows terminates descendants when this worker exits or is killed."""
    if os.name != 'nt':
        return None  # Parent establishes and cleans the POSIX process group.
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                    'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError('Unable to create OCR process containment')
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise OSError('Unable to configure OCR process containment')
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise OSError('Unable to apply OCR process containment')
    return handle  # Keep the job handle open for the entire worker lifetime.


def main():
    _job_handle = contain_process_tree()
    import pypdfium2 as pdfium

    language, indices = sys.argv[1], [int(item) for item in sys.argv[2].split(',')]
    if language not in ('eng', 'eng+ara') or not 1 <= len(indices) <= MAX_PAGES or len(set(indices)) != len(indices):
        raise ValueError('Invalid OCR request')
    path = os.environ.get('TAWZEEF_TESSERACT_PATH') or shutil.which('tesseract')
    if not path and os.name == 'nt':
        path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if not path or not os.path.isfile(path):
        raise ValueError('OCR engine unavailable')
    if not _languages_available(path, language):
        raise MissingLanguage()
    document = pdfium.PdfDocument(sys.stdin.buffer.read())
    result = []
    try:
        for index in indices:
            if index < 0 or index >= len(document):
                raise ValueError('Invalid page')
            page = document[index]
            try:
                width, height = page.get_size()
                w, h = int(width * 2 + 1), int(height * 2 + 1)
                if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE and w * h <= MAX_PIXELS):
                    raise ValueError('Page exceeds OCR limits')
                bitmap = page.render(scale=2)
                try:
                    output = io.BytesIO()
                    bitmap.to_pil().save(output, format='PNG')
                finally:
                    bitmap.close()
                completed = subprocess.run([path, 'stdin', 'stdout', '-l', language], input=output.getvalue(),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                text = completed.stdout.decode('utf-8').strip()
                if not text or len(text) > MAX_OUTPUT or '\x00' in text:
                    raise ValueError('OCR returned no usable text')
                result.append({'index': index, 'text': text})
            finally:
                page.close()
    finally:
        document.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except MissingLanguage:
        sys.exit(2)
    except Exception:  # noqa: BLE001 - worker must not expose document contents or paths.
        sys.exit(1)
