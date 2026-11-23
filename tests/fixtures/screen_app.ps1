# 화면 자동화 시험용 합성 앱(WinForms) — 가짜 개인정보만 심는다. 실제 업무 화면이 아니다.
# 실행: powershell -NoProfile -ExecutionPolicy Bypass -File tests\fixtures\screen_app.ps1
Add-Type -AssemblyName System.Windows.Forms
$fakeName = '홍길동가짜'; $fakeRrn = '900101-1234567'; $fakeMemo = '가나메디칼 진료메모 비밀'
$f = New-Object Windows.Forms.Form
$f.Text = "VaderAI 시험 화면 $fakeName"      # 창 제목에도 심는다 — 밖으로 나가면 안 된다
$f.Width = 360; $f.Height = 420
function Add($c, $x, $y) { $c.Left = $x; $c.Top = $y; $f.Controls.Add($c); $c }
$l = Add (New-Object Windows.Forms.Label) 10 12; $l.Text = '이름'; $l.Width = 60
$name = Add (New-Object Windows.Forms.TextBox) 80 10; $name.Text = $fakeName; $name.Width = 220; $name.Name = 'nameBox'
$l2 = Add (New-Object Windows.Forms.Label) 10 42; $l2.Text = '주민번호'; $l2.Width = 60
$rrn = Add (New-Object Windows.Forms.TextBox) 80 40; $rrn.Text = $fakeRrn; $rrn.Width = 220
$memo = Add (New-Object Windows.Forms.TextBox) 80 70; $memo.Multiline = $true; $memo.Height = 40; $memo.Width = 220; $memo.Text = $fakeMemo
$list = Add (New-Object Windows.Forms.ListBox) 80 115; $list.Height = 40; $list.Width = 220; [void]$list.Items.Add($fakeName); [void]$list.Items.Add('다라상사가짜')
$chk = Add (New-Object Windows.Forms.CheckBox) 80 160; $chk.Text = '확인'
$status = Add (New-Object Windows.Forms.Label) 80 190; $status.Text = '대기'; $status.Name = 'status'
$save = Add (New-Object Windows.Forms.Button) 80 220; $save.Text = '저장'
$save.Add_Click({ $status.Text = '저장됨' })
# 역할이 버튼·탭·링크여도 이름에 개인정보가 들어갈 수 있다 — 승인 목록에 없으면 밖으로 나가면 안 된다 (codex1 P1-A)
$send = Add (New-Object Windows.Forms.Button) 180 220; $send.Text = "$fakeName 에게 보내기"; $send.Width = 150
$link = Add (New-Object Windows.Forms.LinkLabel) 80 255; $link.Text = "$fakeName 상세"
$tabs = Add (New-Object Windows.Forms.TabControl) 80 280; $tabs.Width = 220; $tabs.Height = 60
$t1 = New-Object Windows.Forms.TabPage; $t1.Text = "$fakeName 진료"; [void]$tabs.TabPages.Add($t1)
$t2 = New-Object Windows.Forms.TabPage; $t2.Text = '목록'; [void]$tabs.TabPages.Add($t2)
[void]$f.ShowDialog()
