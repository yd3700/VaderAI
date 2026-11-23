// Bounded procedural instrument: orbit geometry, spherical particles and one breathing nucleus.
let previous=0,phase=0;
function draw(time){
 requestAnimationFrame(draw);
 if(document.hidden)return;
 const dt=Math.min((time-previous)/1000,.05)||.016;previous=time;
 if(!state.motion)phase+=dt*(agent.dataset.state==='running'?1.65:agent.dataset.state==='paused'?.35:1);
 const mode=agent.dataset.state;const rgb=mode==='confirm'||mode==='stopped'?'255,179,93':mode==='complete'?'116,223,187':mode==='paused'?'162,173,188':(taskRunes[agent.querySelector('.agent-mark').dataset.rune]?.rgb || '240,43,58');
 ctx.clearRect(0,0,640,640);ctx.save();ctx.translate(320,305);
 let glow=ctx.createRadialGradient(0,0,22,0,0,232);glow.addColorStop(0,'rgba('+rgb+',.12)');glow.addColorStop(.5,'rgba('+rgb+',.025)');glow.addColorStop(1,'rgba('+rgb+',0)');ctx.fillStyle=glow;ctx.fillRect(-300,-300,600,600);
 // precise outer index marks
 for(let i=0;i<100;i++){let a=i*Math.PI*2/100;ctx.beginPath();ctx.strokeStyle='rgba(163,178,197,'+(i%5===0?.4:.13)+')';ctx.lineWidth=i%5===0?1.1:.6;let r=i%5===0?250:255;ctx.moveTo(Math.cos(a)*r,Math.sin(a)*r);ctx.lineTo(Math.cos(a)*260,Math.sin(a)*260);ctx.stroke()}
 [230,204,164].forEach((r,j)=>{ctx.save();ctx.rotate(phase*(j===1?-.12:.09)*(j+1));for(let k=0;k<4;k++){let a=k*Math.PI/2;ctx.beginPath();ctx.strokeStyle='rgba('+rgb+','+(j===1?.38:.65)+')';ctx.lineWidth=j===1?1:2;ctx.arc(0,0,r,a,a+Math.PI*(j===2?.26:.36));ctx.stroke()}ctx.restore()});
 // rotating point cloud, computed rather than loaded imagery
 for(let i=0;i<210;i++){let y=1-2*(i+.5)/210,lat=Math.sqrt(1-y*y),theta=i*2.39996+phase*.13;let x=Math.cos(theta)*lat,z=Math.sin(theta)*lat;let xx=x*118,yy=y*118;let tilt=.3,py=yy*Math.cos(tilt)-z*118*Math.sin(tilt);ctx.beginPath();ctx.fillStyle='rgba('+rgb+','+(.1+(z+1)*.27)+')';ctx.arc(xx,py,.65+(z+1)*.65,0,Math.PI*2);ctx.fill()}
 for(let j=0;j<3;j++){ctx.save();ctx.rotate(phase*.05+j*1.1);ctx.beginPath();ctx.strokeStyle='rgba('+rgb+',.16)';ctx.lineWidth=.8;ctx.ellipse(0,0,132,54,0,0,Math.PI*2);ctx.stroke();ctx.restore()}
 for(let i=0;i<8;i++){let a=phase*.12+i*.785,r=185+Math.sin(i*2)*27;ctx.beginPath();ctx.fillStyle='rgba('+rgb+',.9)';ctx.arc(Math.cos(a)*r,Math.sin(a)*r,2,0,Math.PI*2);ctx.fill()}
 ctx.restore();
}

requestAnimationFrame(draw);
