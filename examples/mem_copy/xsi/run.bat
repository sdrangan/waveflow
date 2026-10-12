@echo off
rem run.bat <top> <tb_basename> [trace] [all|build|rtl|tb|run] [vectors_dir] — XSI flow
rem (xvlog -> xelab -dll -> g++ BFM -> run) for a generated free-running mem-stream kernel.  Adapted
rem from the interleaver sandbox xsi_task/run.bat.
rem   run.bat mem_r_stream mem_r_bfm_tb
rem   run.bat mem_w_stream mem_w_bfm_tb
rem
rem Pass `trace` to also elaborate vcd_dumper_<top>.v as a SECOND top, whose $dumpvars writes
rem <top>_trace.vcd.  The XSI top -- and so every BFM port number -- is untouched, so the cycle
rem counts are identical either way; only the dump is added.  The dumper is per-top because one xsi/
rem directory can serve several (examples/interleaver/xsi builds three), and a dumper naming a scope
rem that is not part of THIS elaboration is a hard error.  The traced design is its own snapshot,
rem xsim.dir\<top>_trace, so a traced and an untraced build coexist; the run phase points the
rem testbench at it through WF_XSI_DESIGN (xsi_bfm.h).
rem   run.bat mem_copy mem_copy_bfm_tb trace
rem
rem The verb picks the phases (plans/incremental_xsi.md); the default, `all`, is every phase:
rem   rtl    compile the RTL and elaborate the snapshot
rem   tb     compile and link the testbench
rem   build  rtl + tb
rem   run    run the testbench that is already built, against the snapshot already elaborated
rem   all    build + run
rem Any other argument is the VECTORS DIRECTORY of a run: the testbench's "vectors/..." bundles are
rem read from and written to it instead (WF_VECTORS_DIR, xsi_bundle.h), so two runs of one snapshot
rem need not overwrite each other's outputs.
rem   run.bat mem_copy mem_copy_bfm_tb run runs\p3
rem
rem Nothing here decides whether a phase is needed: waveflow.build.xsi_snapshot.XsiSnapshot does,
rem from content stamps it writes after a successful phase.  Each phase deletes its own outputs --
rem and the stamp that vouches for them -- before it rebuilds, so a failed build cannot leave an old
rem artifact that a stamp still describes.
cd /d "%~dp0"
set VIV=C:\Xilinx\2025.1\Vivado
set MINGW=%VIV%\tps\mingw\6.2.0\win64.o\nt
set TOP=%~1
set TB=%~2
set TRACE=
set VERB=all
set VEC=
shift
shift
:parse
if "%~1"=="" goto parsed
if /I "%~1"=="trace" (set TRACE=1) else if /I "%~1"=="all" (set VERB=all) else if /I "%~1"=="build" (set VERB=build) else if /I "%~1"=="rtl" (set VERB=rtl) else if /I "%~1"=="tb" (set VERB=tb) else if /I "%~1"=="run" (set VERB=run) else (set "VEC=%~1")
shift
goto parse
:parsed
set SNAP=%TOP%
if defined TRACE set SNAP=%TOP%_trace
set DO_RTL=
set DO_TB=
set DO_RUN=
if /I "%VERB%"=="all" set DO_RTL=1
if /I "%VERB%"=="all" set DO_TB=1
if /I "%VERB%"=="all" set DO_RUN=1
if /I "%VERB%"=="build" set DO_RTL=1
if /I "%VERB%"=="build" set DO_TB=1
if /I "%VERB%"=="rtl" set DO_RTL=1
if /I "%VERB%"=="tb" set DO_TB=1
if /I "%VERB%"=="run" set DO_RUN=1
set PATH=%~dp0xsim.dir\%SNAP%;%MINGW%\bin;%VIV%\lib\win64.o;%VIV%\bin;%PATH%
rem WF_PHASE lines time each phase (waveflow.build.trace_steps.run_xsi).  They sit outside every
rem if/else block: %time% inside a block is expanded once, when the block is parsed.  That is also
rem why the phases are skipped with goto rather than wrapped in if blocks.
if not defined DO_RTL goto after_rtl
echo WF_PHASE compile_rtl %time%
echo --- xvlog RTL (%TOP%) ---
call %VIV%\bin\xvlog -f rtl_%TOP%.f
echo xvlog errorlevel=%ERRORLEVEL%
echo WF_PHASE elaborate %time%
if exist xsim.dir\%SNAP% rd /s /q xsim.dir\%SNAP%
if defined TRACE goto elab_trace
echo --- xelab -dll ---
call %VIV%\bin\xelab work.%TOP% -dll -s %SNAP% -debug typical
goto elab_done
:elab_trace
echo --- xvlog vcd_dumper_%TOP% ---
call %VIV%\bin\xvlog vcd_dumper_%TOP%.v
echo --- xelab -dll [+ vcd_dumper_%TOP%] ---
call %VIV%\bin\xelab work.%TOP% work.vcd_dumper_%TOP% -dll -s %SNAP% -debug typical
:elab_done
echo xelab errorlevel=%ERRORLEVEL%
:after_rtl
if not defined DO_TB goto after_tb
echo WF_PHASE compile_tb %time%
echo --- g++ BFM tb (%TB%) ---
if exist %TB%.exe del /q %TB%.exe
if exist %TB%.o del /q %TB%.o
if exist %TB%.wf_stamp.json del /q %TB%.wf_stamp.json
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include -O3 -c -o xsi_loader.o xsi_loader.cpp
rem WF_TB_CXXFLAGS: extra flags for the testbench only (a software host's schema headers need
rem Vitis's include dir -- waveflow.build.xsi_workspace sets it); unset, it expands to nothing.
call %MINGW%\bin\g++.exe -I%VIV%\data\xsim\include %WF_TB_CXXFLAGS% -O3 -c -o %TB%.o %TB%.cpp
call %MINGW%\bin\g++.exe -o %TB%.exe %TB%.o xsi_loader.o
echo gpp errorlevel=%ERRORLEVEL%
:after_tb
if not defined DO_RUN goto after_run
set WF_VECTORS_DIR=
set WF_XSI_DESIGN=
if defined VEC set "WF_VECTORS_DIR=%VEC%"
if defined TRACE set WF_XSI_DESIGN=xsim.dir/%SNAP%/xsimk.dll
echo WF_PHASE simulate %time%
echo --- run ---
.\%TB%.exe
echo XSI_EXITCODE=%ERRORLEVEL%
:after_run
echo WF_PHASE end %time%
