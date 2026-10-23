# 3-D steel building, 2 x 2 bays, 2 stories -- kip, in, sec
# X: special moment frames on grid lines Y1 and Y3; Y: one chevron SCBF bay on X1 and X3; gravity beams pinned.
wipe
model BasicBuilder -ndm 3 -ndf 6
set ft 12.0
set bayX [expr 30*$ft]
set bayY [expr 25*$ft]
set H1 [expr 15*$ft]
set H2 [expr 13*$ft]
set E 29000.0
set Fy 50.0

# nodes: tag = 100*level + 10*i + j
set zs [list 0.0 $H1 [expr $H1+$H2]]
for {set k 0} {$k <= 2} {incr k} {
  for {set i 0} {$i <= 2} {incr i} {
    for {set j 0} {$j <= 2} {incr j} {
      node [expr 100*$k + 10*$i + $j] [expr $i*$bayX] [expr $j*$bayY] [lindex $zs $k]
    }
  }
}
fixZ 0.0 1 1 1 1 1 1

# materials and fibre sections
uniaxialMaterial Steel02 1 $Fy $E 0.01 20 0.925 0.15
proc Wsec {tag d bf tf tw} {
  set y1 [expr $d/2.0]; set y2 [expr $d/2.0 - $tf]
  section Fiber $tag -GJ 1.0e8 {
    patch rect 1 8 1  $y2 [expr -$bf/2.0] $y1 [expr $bf/2.0]
    patch rect 1 8 1 [expr -$y1] [expr -$bf/2.0] [expr -$y2] [expr $bf/2.0]
    patch rect 1 8 1 [expr -$y2] [expr -$tw/2.0] $y2 [expr $tw/2.0]
  }
}
Wsec 1 14.0 14.5 0.71 0.44      ;# W14X90 column
Wsec 2 24.3 9.07 0.875 0.515   ;# W24X94 moment-frame beam
Wsec 3 17.9 6.02 0.525 0.315   ;# W18X40 gravity beam
section Fiber 4 -GJ 1.0e7 {
  patch rect 1 4 1 3.5 -4.0 4.0 4.0
  patch rect 1 4 1 -4.0 -4.0 -3.5 4.0
  patch rect 1 1 4 -3.5 -4.0 3.5 -3.5
  patch rect 1 1 4 -3.5 3.5 3.5 4.0
}                               ;# HSS8X8X1/2 (nominal t 0.5 in)
beamIntegration Lobatto 1 1 5
beamIntegration Lobatto 2 2 5
beamIntegration Lobatto 3 3 5
beamIntegration Lobatto 4 4 5
geomTransf PDelta 1 0 1 0       ;# columns: strong axis resists X
geomTransf Linear 2 0 0 1       ;# beams
geomTransf Corotational 3 0 0 1 ;# braces (X-Z plane braces use vecxz 0 1 0 below)
geomTransf Corotational 4 0 1 0

set ele 1
# columns
for {set k 1} {$k <= 2} {incr k} {
  for {set i 0} {$i <= 2} {incr i} {
    for {set j 0} {$j <= 2} {incr j} {
      element forceBeamColumn $ele [expr 100*($k-1)+10*$i+$j] [expr 100*$k+10*$i+$j] 1 1
      incr ele
    }
  }
}
# beams along X: moment frames on Y1 (j=0) and Y3 (j=2); gravity beams on Y2, pinned with equalDOF ends
set pin 5000
for {set k 1} {$k <= 2} {incr k} {
  for {set j 0} {$j <= 2} {incr j} {
    for {set i 0} {$i < 2} {incr i} {
      set a [expr 100*$k+10*$i+$j]; set b [expr 100*$k+10*($i+1)+$j]
      if {$j != 1} {
        element forceBeamColumn $ele $a $b 2 2
      } else {
        node $pin {*}[nodeCoord $a]; node [expr $pin+1] {*}[nodeCoord $b]
        equalDOF $a $pin 1 2 3 4 6; equalDOF $b [expr $pin+1] 1 2 3 4 6
        element forceBeamColumn $ele $pin [expr $pin+1] 2 3
        incr pin 2
      }
      incr ele
    }
  }
}
# beams along Y: all gravity (pinned through equalDOF), split at mid-span where the chevron braces meet (X1 and X3)
for {set k 1} {$k <= 2} {incr k} {
  for {set i 0} {$i <= 2} {incr i} {
    for {set j 0} {$j < 2} {incr j} {
      set a [expr 100*$k+10*$i+$j]; set b [expr 100*$k+10*$i+$j+1]
      node $pin {*}[nodeCoord $a]; node [expr $pin+1] {*}[nodeCoord $b]
      equalDOF $a $pin 1 2 3 5 6; equalDOF $b [expr $pin+1] 1 2 3 5 6
      if {$i != 1 && $j == 0} {
        set mid [expr 900+100*$k+$i]
        node $mid [expr $i*$bayX] [expr 0.5*$bayY] [lindex $zs $k]
        element forceBeamColumn $ele $pin $mid 2 3; incr ele
        element forceBeamColumn $ele $mid [expr $pin+1] 2 3
      } else {
        element forceBeamColumn $ele $pin [expr $pin+1] 2 3
      }
      incr pin 2; incr ele
    }
  }
}
# chevron braces, first story, bays X1 and X3 between Y1 and Y2: 4 elements each with an L/500 camber
foreach i {0 2} {

  foreach {jb} {0 1} {
    set base [expr 10*$i+$jb]
    set x [expr $i*$bayX]
    set y0 [expr $jb*$bayY]; set y1 [expr 0.5*$bayY]
    set z1 $H1
    set prev $base
    for {set s 1} {$s <= 3} {incr s} {
      set f [expr $s/4.0]
      set off [expr ($s==2) ? (sqrt(pow($y1-$y0,2)+pow($z1,2))/500.0) : 0.0]
      set n [expr 2000+100*$i+10*$jb+$s]
      node $n [expr $x+$off] [expr $y0+$f*($y1-$y0)] [expr $f*$z1]
      element forceBeamColumn $ele $prev $n 4 4; incr ele
      set prev $n
    }
    element forceBeamColumn $ele $prev [expr 1000+$i] 4 4; incr ele
  }
}
# floors: rigid diaphragms with the mass on the master node
for {set k 1} {$k <= 2} {incr k} {
  set m [expr 9000+$k]
  node $m [expr $bayX] [expr $bayY] [lindex $zs $k]
  fix $m 0 0 1 1 1 0
  set slaves {}
  for {set i 0} {$i <= 2} {incr i} { for {set j 0} {$j <= 2} {incr j} { lappend slaves [expr 100*$k+10*$i+$j] } }
  rigidDiaphragm 3 $m {*}$slaves
  if {$k == 1} { mass $m 0.8 0.8 0.0 0.0 0.0 250000.0 } else { mass $m 0.6 0.6 0.0 0.0 0.0 190000.0 }
}

# gravity: 1.05 D + 0.25 L as uniform loads on the moment-frame beams
timeSeries Linear 1
pattern Plain 1 1 {
  for {set e 10} {$e < 70} {incr e} {
    if {[catch {eleLoad -ele $e -type -beamUniform 0.0 -0.08}]} {}
  }
}
constraints Transformation
numberer RCM
system UmfPack
test NormDispIncr 1.0e-8 10
algorithm Newton
integrator LoadControl 0.1
analysis Static
set ok [analyze 10]
loadConst -time 0.0
set lam [eigen 3]
puts "T1 = [expr 2*3.14159/sqrt([lindex $lam 0])]"

# pushover in X, displacement control to 2 % drift
pattern Plain 2 1 { load 201 1.0 0 0 0 0 0; load 101 0.6 0 0 0 0 0 }
integrator DisplacementControl 201 1 0.05
analysis Static
set target [expr 0.02*($H1+$H2)]
while {[nodeDisp 201 1] < $target} {
  set ok [analyze 1]
  if {$ok != 0} { break }
}
puts "pushover done"
