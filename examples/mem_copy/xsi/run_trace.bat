@echo off
rem run_trace.bat <top> <tb_basename> -- Test A variant of run.bat: identical flow, plus a second
rem elaborated top (vcd_dumper) whose $dumpvars writes a VCD of the DUT boundary signals.
rem Kept separate from run.bat so the committed gate flow is untouched.
rem   run_trace.bat mem_copy mem_copy_bfm_tb
cd /d "%~dp0"
set VIV=C:\Xilinx\2025.1\Vivado
set MINGW=%VIV%\tps\mingw\6.2.0\win64.o\nt
set TOP=%1
set TB=%2
set PATH=%~dp0xsim.dir\%TOP%;%MINGW%\bin;%VIV%\lib\win64.o;%VIV%\bin;%PATH%
echo --- xvlog RTL (%TOP%) ---
call %VIV%\bin\xvlog -f rtl_%TOP%.f
echo xvlog errorlevel=%ERRORLEVEL%
echo --- xvlog vcd_dumper ---
call %VIV%\bin\xvlog vcd_dumper.v
echo xvlog_dumper errorlevel=%ERRORLEVEL%
echo --- xelab -dll (two tops: %TOP% + vcd_dumper) ---
call %VIV%\bin\xelab work.%TOP% work.vcd_dumper -dll -s %TOP% -debug typical
echo xelab errorlevel=%ERRORLEVEL%
echo --- g++ BFM tb (%TB%) ---
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o xsi_loader.o xsi_loader.cpp
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o %TB%.o %TB%.cpp
call %MINGW%\bin\g++.exe -o %TB%.exe %TB%.o xsi_loader.o
echo gpp errorlevel=%ERRORLEVEL%
echo --- run ---
.\%TB%.exe
echo XSI_EXITCODE=%ERRORLEVEL%
