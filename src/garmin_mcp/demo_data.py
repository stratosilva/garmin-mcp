"""Deterministic fictional demo. Never imports a client or reads account data."""
import datetime as dt
import math


def demo_data(today=None):
    today = today or dt.date.today()
    def day(ago):
        return (today - dt.timedelta(days=ago)).isoformat()
    def label(date):
        return dt.date.fromisoformat(date).strftime('%b %d')
    nights = []
    for i in range(30):
        date = day(29-i)
        hours = round(7.1 + .65*math.sin(i*.7), 1)
        nights.append(dict(date=date, label=label(date), hours=hours, score=round(76+10*math.sin(i*.7)),
                           efficiency=93, bedTime=23+.3*math.sin(i), wakeTime=6.6+.3*math.sin(i),
                           stages=dict(deep=80, rem=100, light=round(hours*60)-180, awake=24)))
    nights[-1].update(hours=6.4, score=68, stages=dict(deep=62,rem=86,light=236,awake=32))
    fitness=[]
    for i in range(732):
        value=25+9*math.sin(i/65)+5*math.sin(i/24)
        if i>690: value=16+(i-690)*.21+math.sin(i)*.25
        fitness.append(dict(date=day(731-i),label=label(day(731-i)),fitness=round(value,2),fatigue=round(value+4*math.sin(i/7),2),form=round(-4*math.sin(i/7),2),load=[0,32,18,0,48,22,35][i%7]))
    recent=[]
    for i,(name,sport,minutes,km,hr,cal) in enumerate([
        ('Park recovery walk','walk',32,2.8,104,145),('Upper-body strength','strength',48,0,115,285),
        ('Easy riverside run','run',38,5.7,138,425),('Indoor endurance ride','bike',50,21,128,390),
        ('Full-body strength','strength',55,0,120,340),('Weekend long run','run',72,11.2,145,810)]):
        recent.append(dict(activityId=9001+i,name=name,sport=sport,min=minutes,km=km,hr=hr,cal=cal,
                           date=day(i),start=day(i)+'T08:30:00',isStrength=sport=='strength',source='garmin'))
    monday=today-dt.timedelta(days=today.weekday())
    weeks=[]
    for i in range(12):
        start=monday-dt.timedelta(weeks=11-i)
        days=[dict(date=(start+dt.timedelta(days=j)).isoformat(),label=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][j],effort=(round([25,0,38,18,0,42,15][j]*(.8+.3*math.sin(i*.8))) if start+dt.timedelta(days=j)<=today else None)) for j in range(7)]
        effort=sum(x['effort'] or 0 for x in days)
        activities=[dict(name='Easy run' if j%2 else 'Strength session',sport='run' if j%2 else 'strength',date=x['date'],min=35+j*3,hr=132,effort=x['effort'],garminLoad=80,zonePart=round(x['effort']*.7,1),loadPart=round(x['effort']*.3,1)) for j,x in enumerate(days) if x['effort']]
        weeks.append(dict(label=start.strftime('%b %d'),rangeLabel=start.strftime('%b %d')+' – '+(start+dt.timedelta(days=6)).strftime('%b %d'),effort=effort,rangeLow=100,rangeHigh=175,state='within',partial=i==11,daysElapsed=today.weekday()+1,projected=138,capacity=145,garminLoad=320,days=days,activities=activities))
    muscles=[]
    for i in range(12):
        rows=[]
        for j,key in enumerate(['chest','back','delts','biceps','triceps','core','quads','glutes','hamstrings','calves']):
            direct=[6,9,5,3,4,4,2,3,2,1][j]+(i%3)
            rows.append(dict(key=key,label=key.title(),direct=direct,indirectStrength=2,cardio=1,movement=1,total=direct+4))
        muscles.append(dict(label=weeks[i]['rangeLabel'],isCurrent=i==11,muscles=rows,sources=dict(strengthSets=28,cardioSessions=3,cardioMinutes=160,steps=58000,floors=49),exerciseBreakdown=[
            dict(exercise='Dumbbell bench press',sets=3,mapped=True,muscles=[dict(label='Chest',role='primary',volumeCreditPct=100),dict(label='Triceps',role='assisting',volumeCreditPct=50)]),
            dict(exercise='Seated cable row',sets=3,mapped=True,muscles=[dict(label='Back',role='primary',volumeCreditPct=100),dict(label='Biceps',role='assisting',volumeCreditPct=50)])]))
    definitions=[dict(id='demo_knee',name='Right knee - patellar tendon',color='#1683ff',enabled=True),dict(id='demo_shoulder',name='Plantar fasciitis left foot',color='#f08c00',enabled=True),dict(id='demo_ankle',name='Ankle recovery',color='#e5484d',enabled=False)]
    records=[dict(date=day(29-i),demo_knee=max(1,round(4-i/12+math.sin(i)*.7)),demo_shoulder=max(0,round(3-i/11)),demo_ankle=max(0,3-i//5),notes=('Shorter walk felt comfortable. Keeping the next run easy.' if i==29 else 'Mobility work helped today.' if i==24 else '')) for i in range(30)]
    # Mirror the personal dashboard's seven contributors using fictional inputs.
    sleep_hours = sum(night['hours'] for night in nights[-14:])
    yesterday_effort = next(d['effort'] for week in weeks for d in week['days'] if d['date'] == day(1))
    previous_day_pct = round(max(0, min(1, 1 - (yesterday_effort / (145 / 7) - 1) / 3)) * 100)
    contributors = [
        dict(key='restingHr', label='Resting heart rate', percent=84, detail='54 bpm', note='baseline 52 bpm'),
        dict(key='hrvBalance', label='HRV balance', percent=92, detail='Balanced', note='52 ms 7-day average'),
        dict(key='sleep', label='Sleep', percent=nights[-1]['score'], detail='6.4 h', note='sleep score 68'),
        dict(key='sleepBalance', label='Sleep balance', percent=round(sleep_hours / (7.5 * 14) * 100), detail=f'{round(sleep_hours)} h over 14 nights', note='need 7.5 h a night'),
        dict(key='sleepRegularity', label='Sleep regularity', percent=82, detail='±24 min', note='drift in mid-sleep time'),
        dict(key='previousDay', label='Previous day activity', percent=previous_day_pct, detail=f'{yesterday_effort} effort points', note='yesterday against a typical day'),
        dict(key='activityBalance', label='Activity balance', percent=92, detail='In range', note='this week against your effort band'),
    ]
    for contributor in contributors:
        value = contributor['percent']
        contributor['grade'] = 'optimal' if value >= 85 else 'good' if value >= 70 else 'attention'
    return dict(name='Miles Ahead',date=day(0),generatedAt=day(0)+' · fictional scenario',
        wellness=dict(bodyBattery=dict(current=58,high=82),steps=dict(value=6240,goal=9000,avg7=8240),restingHr=dict(value=54,avg7=52,min=49,max=56),stress=dict(avg=28,max=64),distanceKm=4.7,
            readiness=dict(score=62),sleep=dict(score=68),hrv=dict(value=48,weeklyAvg=52,baselineLow=46,baselineHigh=64,status='BALANCED'),floors=dict(value=7,goal=10,avg7=9),vo2maxRun=47,vo2RatingAge=36,vo2maxRunDate=day(2),weight=dict(kg=76.8),
            calories=dict(total=2180,active=430,bmr=1750,avg7=2460),trainingLoad=dict(status='OPTIMAL',acute=370,chronic=410,acwr=.9),intensity=dict(total=172,goalWeek=150,moderate=92,vigorous=40)),
        workoutRecommendation=dict(label='Keep it easy',level='easy',text='Prioritise recovery; keep today’s movement comfortable.'),
        bodyBatterySeries=[[int(dt.datetime.combine(today-dt.timedelta(days=6),dt.time(),tzinfo=dt.timezone.utc).timestamp()*1000)+i*3600000,round(51+27*math.cos((i%24-7)/24*2*math.pi)+3*math.sin(i/18))] for i in range(160)],
        sports={key:dict(hasData=True,week=dict(km=34 if key=='run' else 64,sessions=3),month=dict(km=112 if key=='run' else 245),last=dict(km=5.7 if key=='run' else 21,min=38 if key=='run' else 50,hr=138,date=day(2))) for key in ['run','bike']},
        workouts=dict(hasData=True,week=dict(sessions=2,min=103,cal=625),last=recent[1],lastStrength=recent[1],types=[dict(name='Strength',count=2)]),
        recent=recent,strength=dict(activities=[]),muscleVolume=dict(weeks=muscles,currentIndex=11,formula='Direct sets are counted separately from estimated assisting, cardio and daily movement exposure.'),
        relativeEffort=dict(current=weeks[-1]['effort'],weeks=weeks,formula='Combines time in heart-rate zones and an intensity component.',rangeModel='An individual baseline provides context for each week.'),fitnessSeries=fitness,
        recovery=dict(contributors=contributors,
            nights=nights,lastNight=nights[-1],sleepNeedHours=7.5,needModel='Sleep need shown for this fictional athlete.',debtModel='Rolling estimate, with recovery from longer nights.',regularity=dict(score=82,deviationMinutes=24),
            debt=dict(minutes=108,level='moderate',series=[dict(label=x['label'],minutes=round(70+40*math.sin(i/4)+i)) for i,x in enumerate(nights)])),
        hrvSeries=[dict(date=day(59-i),label=label(day(59-i)),value=round(53+6*math.sin(i/5)+2*math.sin(i)),baselineLow=46,baselineHigh=64,status='BALANCED') for i in range(60)],
        trainingLoadTrend=[dict(label=(today-dt.timedelta(days=6-i)).strftime('%a'),load=[85,0,110,62,0,78,35][i]) for i in range(7)],hrZonesWeek=[48,125,39,14,3],
        body=dict(metrics={key:dict(value=v,delta=delta,date=day(3)) for key,v,delta in [('weight_kg',76.8,-.4),('fat_pct',18.5,-.3),('muscle_pct',77.3,.3),('body_water_pct',57.1,.2)]}),injuries=dict(definitions=definitions,records=records))
