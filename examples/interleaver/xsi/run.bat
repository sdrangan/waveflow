@echo off
rem run.bat <top> <tb_basename> — XSI flow (xvlog -> xelab -dll -> g++ BFM -> run) for a generated
rem free-running mem-stream kernel.  Adapted from the interleaver sandbox xsi_task/run.bat.
rem   run.bat mem_r_stream mem_r_bfm_tb
rem   run.bat mem_w_stream mem_w_bfm_tb
cd /d "%~dp0"
set VIV=C:\Xilinx\2025.1\Vivado
set MINGW=%VIV%\tps\mingw\6.2.0\win64.o\nt
set TOP=%1
set TB=%2
set PATH=%~dp0xsim.dir\%TOP%;%MINGW%\bin;%VIV%\lib\win64.o;%VIV%\bin;%PATH%
echo --- xvlog RTL (%TOP%) ---
call %VIV%\bin\xvlog -f rtl_%TOP%.f
echo xvlog errorlevel=%ERRORLEVEL%
echo --- xelab -dll ---
call %VIV%\bin\xelab work.%TOP% -dll -s %TOP% -debug typical
echo xelab errorlevel=%ERRORLEVEL%
echo --- g++ BFM tb (%TB%) ---
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o xsi_loader.o xsi_loader.cpp
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o %TB%.o %TB%.cpp
call %MINGW%\bin\g++.exe -o %TB%.exe %TB%.o xsi_loader.o
echo gpp errorlevel=%ERRORLEVEL%
echo --- run ---
.\%TB%.exe
echo XSI_EXITCODE=%ERRORLEVEL%
