try {
    cd "d:\Workspace\Python\c69-router"
    # Execute python and write logs
    & "C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > "d:\Workspace\Python\c69-router\server.log" 2>&1
} catch {
    $_ | Out-File "d:\Workspace\Python\c69-router\error_elevated.txt"
}
